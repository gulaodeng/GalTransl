import json, time, asyncio, os, traceback
from opencc import OpenCC
from typing import Optional
from GalTransl.COpenAI import COpenAITokenPool
from GalTransl.ConfigHelper import CProxyPool
from GalTransl import LOGGER, LANG_SUPPORTED
from sys import exit
from GalTransl.ConfigHelper import (
    CProjectConfig,
)
from random import choice
from GalTransl.CSentense import CSentense, CTransList
from GalTransl.Cache import get_transCache_from_json, save_transCache_to_json
from GalTransl.Dictionary import CGptDict
from GalTransl.StringUtils import extract_code_blocks
from GalTransl.Backend.Prompts import (
    GPT4_CONF_PROMPT,
    GPT4_TRANS_PROMPT,
    GPT4_SYSTEM_PROMPT,
    GPT4_PROOFREAD_PROMPT,
)
from GalTransl.Backend.Prompts import (
    GPT4Turbo_SYSTEM_PROMPT,
    GPT4Turbo_TRANS_PROMPT,
    GPT4Turbo_CONF_PROMPT,
    GPT4Turbo_PROOFREAD_PROMPT,
)

NAME_PROMPT3 = "and `name`(if have) "


class CGPT4Translate:
    # init
    def __init__(
        self,
        config: CProjectConfig,
        eng_type: str,
        proxy_pool: Optional[CProxyPool],
        token_pool: COpenAITokenPool,
    ):
        """
        根据提供的类型、配置、API 密钥和代理设置初始化 Chatbot 对象。

        Args:
            config (dict, 可选): 使用 非官方API 时提供 的配置字典。默认为空字典。
            apikey (str, 可选): 使用 官方API 时的 API 密钥。默认为空字符串。
            proxy (str, 可选): 使用 官方API 时的代理 URL，非官方API的代理写在config里。默认为空字符串。

        Returns:
            None
        """
        self.eng_type = eng_type
        self.last_file_name = ""
        self.restore_context_mode = config.getKey("gpt.restoreContextMode")
        self.retry_count = 0
        # 记录确信度
        if val := config.getKey("gpt.recordConfidence"):
            self.record_confidence = val
        else:
            self.record_confidence = False
        # 源语言
        if val := config.getKey("sourceLanguage"):
            self.source_lang = val
        else:
            self.source_lang = "ja"
        if self.source_lang not in LANG_SUPPORTED.keys():
            raise ValueError("错误的源语言代码：" + self.source_lang)
        else:
            self.source_lang = LANG_SUPPORTED[self.source_lang]
        # 目标语言
        if val := config.getKey("targetLanguage"):
            self.target_lang = val
        else:
            self.target_lang = "zh-cn"
        if self.target_lang not in LANG_SUPPORTED.keys():
            raise ValueError("错误的目标语言代码：" + self.target_lang)
        else:
            self.target_lang = LANG_SUPPORTED[self.target_lang]
        # 挥霍token模式
        if val := config.getKey("gpt.fullContextMode"):
            self.full_context_mode = val
        else:
            self.full_context_mode = False
        # 跳过重试
        if val := config.getKey("skipRetry"):
            self.skipRetry = val
        else:
            self.skipRetry = False
        # 流式输出模式
        if val := config.getKey("gpt.streamOutputMode"):
            self.streamOutputMode = val
        else:
            self.streamOutputMode = False
        if val := config.getKey("workersPerProject"):  # 多线程关闭流式输出
            if val > 1:
                self.streamOutputMode = False

        self.tokenProvider = token_pool
        if config.getKey("internals.enableProxy") == True:
            self.proxyProvider = proxy_pool
        else:
            self.proxyProvider = None
            LOGGER.warning("不使用代理")
        # 翻译风格
        if val := config.getKey("gpt.translStyle"):
            self.transl_style = val
        else:
            self.transl_style = "normal"
        self._current_style = ""

        self.init_chatbot(eng_type=eng_type, config=config)  # 模型选择

        if self.transl_style == "auto":
            self._set_gpt_style("precise")
        else:
            self._set_gpt_style(self.transl_style)

        if self.target_lang == "Simplified Chinese":
            self.opencc = OpenCC("t2s.json")
        elif self.target_lang == "Traditional Chinese":
            self.opencc = OpenCC("s2t.json")

        pass

    def init_chatbot(self, eng_type, config):
        if eng_type == "gpt4":
            from GalTransl.Backend.revChatGPT.V3 import Chatbot as ChatbotV3

            self.token = self.tokenProvider.getToken(False, True)
            self.chatbot = ChatbotV3(
                api_key=self.token.token,
                temperature=0.4,
                frequency_penalty=0.2,
                system_prompt=GPT4_SYSTEM_PROMPT,
                engine="gpt-4",
                api_address=self.token.domain + "/v1/chat/completions",
                timeout=30,
            )
            self.chatbot.trans_prompt = GPT4_TRANS_PROMPT
            self.chatbot.proofread_prompt = GPT4_PROOFREAD_PROMPT
            self.chatbot.update_proxy(
                self.proxyProvider.getProxy().addr if self.proxyProvider else None
            )
        elif eng_type == "gpt4-turbo":
            from GalTransl.Backend.revChatGPT.V3 import Chatbot as ChatbotV3

            self.token = self.tokenProvider.getToken(False, True)

            system_prompt = GPT4Turbo_SYSTEM_PROMPT
            self.chatbot = ChatbotV3(
                api_key=self.token.token,
                temperature=0.4,
                frequency_penalty=0.2,
                system_prompt=system_prompt,
                engine="gpt-4-1106-preview",
                api_address=self.token.domain + "/v1/chat/completions",
                timeout=30,
                # response_format="json",
            )
            self.chatbot.trans_prompt = GPT4Turbo_TRANS_PROMPT
            self.chatbot.proofread_prompt = GPT4Turbo_PROOFREAD_PROMPT
            self.chatbot.update_proxy(
                self.proxyProvider.getProxy().addr if self.proxyProvider else None
            )
        elif eng_type == "unoffapi":
            from GalTransl.Backend.revChatGPT.V1 import Chatbot as ChatbotV1

            gpt_config = {
                "model": "gpt-4",
                "paid": True,
                "access_token": choice(
                    config.getBackendConfigSection("ChatGPT")["access_tokens"]
                )["access_token"],
                "proxy": self.proxyProvider.getProxy().addr if self.proxies else None,
            }
            if gpt_config["proxy"] == "":
                del gpt_config["proxy"]
            self.chatbot = ChatbotV1(config=gpt_config)
            self.chatbot.trans_prompt = GPT4_TRANS_PROMPT
            self.chatbot.clear_conversations()

    async def translate(self, trans_list: CTransList, gptdict="", proofread=False):
        input_list = []
        for i, trans in enumerate(trans_list):
            if not proofread:
                tmp_obj = {
                    "id": trans.index,
                    "name": trans.speaker,
                    "src": trans.post_jp,
                }
                if trans.speaker == "":
                    del tmp_obj["name"]
                input_list.append(tmp_obj)
            else:
                tmp_obj = {
                    "id": trans.index,
                    "name": trans.speaker,
                    "src": trans.post_jp,
                    "dst": trans.pre_zh
                    if trans.proofread_zh == ""
                    else trans.proofread_zh,
                }
                if trans.speaker == "":
                    del tmp_obj["name"]

                input_list.append(tmp_obj)
        # dump as jsonline
        input_json = "\n".join(
            [json.dumps(obj, ensure_ascii=False) for obj in input_list]
        )

        prompt_req = (
            self.chatbot.trans_prompt
            if not proofread
            else self.chatbot.proofread_prompt
        )
        prompt_req = prompt_req.replace("[Input]", input_json)
        prompt_req = prompt_req.replace("[Glossary]", gptdict)
        prompt_req = prompt_req.replace("[SourceLang]", self.source_lang)
        prompt_req = prompt_req.replace("[TargetLang]", self.target_lang)
        if self.record_confidence:
            prompt_req = prompt_req.replace("[ConfRecord]", GPT4_CONF_PROMPT)
        else:
            prompt_req = prompt_req.replace("[ConfRecord]", "")
        if '"name"' in input_json:
            prompt_req = prompt_req.replace("[NamePrompt3]", NAME_PROMPT3)
        else:
            prompt_req = prompt_req.replace("[NamePrompt3]", "")
        while True:  # 一直循环，直到得到数据
            try:
                # change token
                if self.eng_type != "unoffapi":
                    self.token = self.tokenProvider.getToken(False, True)
                    self.chatbot.set_api_key(self.token.token)
                # LOGGER.info("->输入：\n" + prompt_req + "\n")
                LOGGER.info(
                    f"->{'翻译输入' if not proofread else '校对输入'}：{gptdict}\n{input_json}\n"
                )
                if self.streamOutputMode:
                    LOGGER.info("->输出：")
                resp = ""
                if self.eng_type != "unoffapi":
                    if not self.full_context_mode:
                        self._del_previous_message()
                    async for data in self.chatbot.ask_stream_async(prompt_req):
                        if self.streamOutputMode:
                            print(data, end="", flush=True)
                        resp += data
                    print(data, end="\n")
                elif self.eng_type == "unoffapi":
                    async for data in self.chatbot.ask_async(prompt_req):
                        if self.streamOutputMode:
                            print(data["message"][len(resp) :], end="", flush=True)
                        resp = data["message"]

                if not self.streamOutputMode:
                    LOGGER.info(f"->输出：\n{resp}")
                else:
                    print("")
            except asyncio.CancelledError:
                raise
            except Exception as ex:
                str_ex = str(ex).lower()
                LOGGER.error(f"-> {str_ex}")
                if "quota" in str_ex:
                    self.tokenProvider.reportTokenProblem(self.token)
                    LOGGER.error(f"-> 余额不足： {self.token.maskToken()}")
                    self.token = self.tokenProvider.getToken(False, True)
                    self.chatbot.set_api_key(self.token.token)
                elif "try again later" in str_ex or "too many requests" in str_ex:
                    LOGGER.warning("-> 请求受限，5分钟后继续尝试")
                    await asyncio.sleep(300)
                    continue
                elif "expired" in str_ex:
                    LOGGER.error("-> access_token过期，请更换")
                    exit()
                elif "try reload" in str_ex:
                    self.reset_conversation()
                    LOGGER.error("-> 报错重置会话")
                    continue

                self._del_last_answer()
                LOGGER.info("-> 报错:%s, 5秒后重试" % ex)
                await asyncio.sleep(5)
                continue

            result_text = resp[resp.find('{"id') :]

            result_text = (
                result_text.replace(", doub:", ', "doub":')
                .replace(", conf:", ', "conf":')
                .replace(", unkn:", ', "unkn":')
            )
            i = -1
            result_trans_list = []
            key_name = "dst" if not proofread else "newdst"
            error_flag = False
            error_message = ""
            for line in result_text.split("\n"):
                try:
                    line_json = json.loads(line)  # 尝试解析json
                    i += 1
                except:
                    if i == -1:
                        LOGGER.error("-> 非json：\n" + line + "\n")
                        error_flag = True
                        continue
                    else:
                        continue

                error_flag = False
                # 本行输出不正常
                if (
                    isinstance(line_json, dict) == False
                    or "id" not in line_json
                    or type(line_json["id"]) != int
                    or i > len(trans_list) - 1
                ):
                    error_message = f"{line}句不无法解析"
                    error_flag = True
                    break
                line_id = line_json["id"]
                if line_id != trans_list[i].index:
                    error_message = f"-> 输出{line_id}句id未对应"
                    error_flag = True
                    break
                if key_name not in line_json or type(line_json[key_name]) != str:
                    error_message = f"第{trans_list[i].index}句找不到{key_name}"
                    error_flag = True
                    break
                # 本行输出不应为空
                if trans_list[i].post_jp != "" and line_json[key_name] == "":
                    error_message = f"-> 第{line_id}句空白"
                    error_flag = True
                    break
                if "/" in line_json[key_name]:
                    if (
                        "／" not in trans_list[i].post_jp
                        and "/" not in trans_list[i].post_jp
                    ):
                        error_message = f"-> 第{line_id}句多余 / 符号：" + line_json[key_name]
                        error_flag = True
                        break

                if "Chinese" in self.target_lang:  # 统一简繁体
                    line_json[key_name] = self.opencc.convert(line_json[key_name])

                if not proofread:
                    trans_list[i].pre_zh = line_json[key_name]
                    trans_list[i].post_zh = line_json[key_name]
                    trans_list[i].trans_by = "GPT-4"
                    if "conf" in line_json:
                        trans_list[i].trans_conf = line_json["conf"]
                    if "doub" in line_json:
                        trans_list[i].doub_content = line_json["doub"]
                    if "unkn" in line_json:
                        trans_list[i].unknown_proper_noun = line_json["unkn"]
                    result_trans_list.append(trans_list[i])
                else:
                    trans_list[i].proofread_zh = line_json[key_name]
                    trans_list[i].proofread_by = "GPT-4"
                    trans_list[i].post_zh = line_json[key_name]
                    result_trans_list.append(trans_list[i])

            if error_flag:
                if self.skipRetry:
                    self.reset_conversation()
                    LOGGER.warning("-> 解析出错但跳过本轮翻译")
                    i = 0 if i < 0 else i
                    while i < len(trans_list):
                        if not proofread:
                            trans_list[i].pre_zh = "Failed translation"
                            trans_list[i].post_zh = "Failed translation"
                            trans_list[i].trans_by = "GPT-4(Failed)"
                        else:
                            trans_list[i].proofread_zh = trans_list[i].pre_zh
                            trans_list[i].post_zh = trans_list[i].pre_zh
                            trans_list[i].proofread_by = "GPT-4(Failed)"
                        result_trans_list.append(trans_list[i])
                        i = i + 1
                    return i, result_trans_list
                else:
                    self._handle_error(error_message)
                    continue
            else:
                if self.transl_style == "auto":
                    self._set_gpt_style("precise")
                self.retry_count = 0

            return i + 1, result_trans_list

    async def batch_translate(
        self,
        filename,
        cache_file_path,
        trans_list: CTransList,
        num_pre_request: int,
        retry_failed: bool = False,
        chatgpt_dict: CGptDict = None,
        proofread: bool = False,
        retran_key: str = "",
    ) -> CTransList:
        _, trans_list_unhit = get_transCache_from_json(
            trans_list,
            cache_file_path,
            retry_failed=retry_failed,
            proofread=proofread,
            retran_key=retran_key,
        )

        # 校对模式多喂上一行
        # if proofread and trans_list_unhit[0].prev_tran != None:
        #    trans_list_unhit.insert(0, trans_list_unhit[0].prev_tran)
        if len(trans_list_unhit) == 0:
            return []
        # 新文件重置chatbot
        if self.last_file_name != filename:
            self.reset_conversation()
            self.last_file_name = filename
            LOGGER.info(f"-> 开始翻译文件：{filename}")
        i = 0

        if (
            self.eng_type != "unoffapi"
            and self.restore_context_mode
            and len(self.chatbot.conversation["default"]) == 1
        ):
            if not proofread:
                self.restore_context(trans_list_unhit, num_pre_request)

        trans_result_list = []
        len_trans_list = len(trans_list_unhit)
        while i < len_trans_list:
            await asyncio.sleep(1)
            trans_list_split = (
                trans_list_unhit[i : i + num_pre_request]
                if (i + num_pre_request < len_trans_list)
                else trans_list_unhit[i:]
            )

            dic_prompt = (
                chatgpt_dict.gen_prompt(trans_list_split)
                if chatgpt_dict != None
                else ""
            )

            num, trans_result = await self.translate(
                trans_list_split, dic_prompt, proofread=proofread
            )

            if num > 0:
                i += num
            result_output = ""
            for trans in trans_result:
                result_output = result_output + repr(trans)
            LOGGER.info(result_output)
            trans_result_list += trans_result
            save_transCache_to_json(trans_list, cache_file_path)
            LOGGER.info(
                f"{filename}: {str(len(trans_result_list))}/{str(len_trans_list)}"
            )

        return trans_result_list

    def _handle_error(self, error_msg: str = "") -> None:
        LOGGER.error(f"-> 错误的输出：{error_msg}")
        self.retry_count += 1
        # 切换模式
        if self.transl_style == "auto":
            self._set_gpt_style("normal")
        # 3次重试则重置会话
        if self.retry_count % 3 == 0:
            self.reset_conversation()
            LOGGER.warning("-> 3次出错重置会话")
            return
        # 10次重试则中止
        if self.retry_count > 10:
            LOGGER.error(f"-> 循环重试超过10次，已中止：{error_msg}")
            exit(-1)
        # 其他情况
        if self.eng_type != "unoffapi":
            self._del_last_answer()
        elif self.eng_type == "unoffapi":
            self.reset_conversation()

    def reset_conversation(self):
        if self.eng_type != "unoffapi":
            self.chatbot.reset()
        elif self.eng_type == "unoffapi":
            self.chatbot.reset_chat()

    def _del_previous_message(self) -> None:
        """删除历史消息，只保留最后一次的翻译结果，节约tokens"""
        if self.eng_type != "unoffapi":
            last_assistant_message = None
            for message in self.chatbot.conversation["default"]:
                if message["role"] == "assistant":
                    last_assistant_message = message
            system_message = self.chatbot.conversation["default"][0]
            if last_assistant_message != None:
                self.chatbot.conversation["default"] = [
                    system_message,
                    last_assistant_message,
                ]
        elif self.eng_type == "unoffapi":
            pass

    def _del_last_answer(self):
        if self.eng_type != "unoffapi":
            # 删除上次输出
            if self.chatbot.conversation["default"][-1]["role"] == "assistant":
                self.chatbot.conversation["default"].pop()
            elif self.chatbot.conversation["default"][-1]["role"] is None:
                self.chatbot.conversation["default"].pop()
            # 删除上次输入
            if self.chatbot.conversation["default"][-1]["role"] == "user":
                self.chatbot.conversation["default"].pop()
        elif self.eng_type == "unoffapi":
            pass

    def _set_gpt_style(self, style_name: str):
        if self.eng_type == "unoffapi":
            return
        if self._current_style == style_name:
            return
        self._current_style = style_name
        if self.transl_style == "auto":
            LOGGER.info(f"-> 自动切换至{style_name}参数预设")
        else:
            LOGGER.info(f"-> 使用{style_name}参数预设")
        # normal default
        temperature, top_p = 1.0, 1.0
        frequency_penalty, presence_penalty = 0.3, 0.0
        if style_name == "precise":
            temperature, top_p = 0.5, 1.0
            frequency_penalty, presence_penalty = 0.3, 0.0
        elif style_name == "normal":
            pass
        if self.eng_type != "unoffapi":
            self.chatbot.temperature = temperature
            self.chatbot.top_p = top_p
            self.chatbot.frequency_penalty = frequency_penalty
            self.chatbot.presence_penalty = presence_penalty

    def restore_context(self, trans_list_unhit: CTransList, num_pre_request: int):
        if self.eng_type != "unoffapi":
            if trans_list_unhit[0].prev_tran == None:
                return
            tmp_context = []
            num_count = 0
            current_tran = trans_list_unhit[0].prev_tran
            while current_tran != None:
                if current_tran.pre_zh == "":
                    current_tran = current_tran.prev_tran
                    continue
                tmp_obj = {
                    "id": current_tran.index,
                    "name": current_tran._speaker,
                    "dst": current_tran.pre_zh,
                }
                if current_tran._speaker == "":
                    del tmp_obj["name"]
                tmp_context.append(tmp_obj)
                num_count += 1
                if num_count >= num_pre_request:
                    break
                current_tran = current_tran.prev_tran

            tmp_context.reverse()
            json_lines = "\n".join(
                [json.dumps(obj, ensure_ascii=False) for obj in tmp_context]
            )
            self.chatbot.conversation["default"].append(
                {
                    "role": "assistant",
                    "content": f"Transl: \n```jsonline\n{json_lines}\n```",
                }
            )
            LOGGER.info("-> 恢复了上下文")

        elif self.eng_type == "unoffapi":
            pass


if __name__ == "__main__":
    pass
