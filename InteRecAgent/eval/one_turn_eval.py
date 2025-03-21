# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

import argparse
import json
import math
import os
import random
import re
import sys
import threading
import time
from datetime import datetime
from typing import *

import numpy as np
import openai
from rapidfuzz import fuzz
from tqdm import tqdm

from llm4crs.agent import CRSAgent
from llm4crs.agent_plan_first import CRSAgentPlanFirst
from llm4crs.agent_plan_first_openai import CRSAgentPlanFirstOpenAI
from llm4crs.buffer import CandidateBuffer
from llm4crs.corups import BaseGallery
from llm4crs.critic import Critic
from llm4crs.environ_variables import *
from llm4crs.mapper import MapTool
from llm4crs.prompt import *
from llm4crs.ranking import RecModelTool
from llm4crs.retrieval import SimilarItemTool, SQLSearchTool
from llm4crs.query import QueryTool
from llm4crs.utils import FuncToolWrapper


def read_jsonl(fpath: str) -> List[Dict]:
    res = []
    with open(fpath, "r", encoding="utf-8") as f:
        for line in f:
            data = json.loads(line)
            res.append(data)
    return res


def write_jsonl(obj, fpath: str):
    with open(fpath, "w", encoding="utf-8") as f:
        for entry in obj:
            json.dump(entry, f)
            f.write("\n")


class Conversation:
    def __init__(self, user_prefix="User", agent_prefix="Assistent"):
        self.user_prefix = user_prefix
        self.agent_prefix = agent_prefix
        self.all_history = []
        self.history = []

    def add_user_msg(self, msg) -> None:
        self.history.append({"role": self.user_prefix, "msg": msg})
        return

    def add_agent_msg(self, msg) -> None:
        self.history.append({"role": self.agent_prefix, "msg": msg})
        return

    @property
    def total_history(self) -> str:
        res = ""
        for h in self.history:
            res += "{}: {}\n".format(h["role"], h["msg"])
        res = res[:-1]
        return res

    @property
    def turns(self) -> int:
        return math.ceil(len(self.history) / 2)

    def __len__(self) -> int:
        return len(self.history)

    def clear(self) -> None:
        if len(self.history) > 0:
            self.all_history.append(
                {"id": len(self.all_history), "conversation": self.history}
            )
        self.history = []

    def dump(self, fpath: str):
        with open(fpath, "w", encoding="utf-8") as f:
            for entry in self.all_history:
                json.dump(entry, f)
                f.write("\n")


class OpenAIBot:
    def __init__(
        self,
        domain: str,
        engine: str,
        api_key: str,
        api_type: str,
        api_base: str,
        api_version: str,
        num_rec: int,
        timeout: int,
        fschat: bool = False,
    ):
        self.domain = domain
        self.engine = engine
        self.api_key = api_key
        self.api_type = api_type
        self.api_base = api_base
        self.api_version = api_version
        self.num_rec = num_rec
        self.timeout = timeout
        self.fschat = fschat

    def run(self, chat_history: str) -> str:
        if len(self.api_type) > 0:
            openai.api_base = self.api_base
            openai.api_version = self.api_version
            openai.api_type = self.api_type
        else:
            pass
        if self.fschat:
            openai.api_base = self.api_base
        openai.api_key = self.api_key
        prompt = "You are a helpful conversational agent who is good at {domain} recommendation. Your task is to recommend {num_rec} {domain}s based on the conversation history."
        sys_msg = {
            "role": "system",
            "content": prompt.format(domain=self.domain, num_rec=self.num_rec),
        }

        usr_prompt = "Here is the conversation history: \n{chat_history}. Now please guess the <item> in the last turn given by assistent. "
        usr_msg = {
            "role": "user",
            "content": usr_prompt.format(chat_history=chat_history),
        }

        msg = [sys_msg, usr_msg]

        retry_cnt = 6
        for retry in range(retry_cnt):
            # runner = TimeoutRunner(openai.ChatCompletion.create, self.timeout)
            try:
                kwargs = {
                    "model": self.engine,
                    "temperature": 0.8,
                    "messages": msg,
                    "max_tokens": 256,
                    "request_timeout": self.timeout,
                }
                if (not self.fschat) and (openai.api_type != "open_ai"):
                    kwargs["engine"] = self.engine
                chat = openai.ChatCompletion.create(**kwargs)
                reply = chat.choices[0].message.content
                break
            except Exception as e:
                print(f"An error occurred while making the API call: {e}")
                reply = "Something went wrong, please retry."
                time.sleep(random.randint(1, 5))
        return reply


class RecBotWrapper:
    def __init__(self, bot: CRSAgent, num_rec):
        self.bot = bot
        self.num_rec = num_rec

    def run(self, chat_history: str):
        response = self.bot.run(
            {
                "input": f"Please give {self.num_rec} recommendations based on the chat history. Do not use candidate storing tool"
            },
            chat_history,
        )
        return response


class StaticAgent:
    def __init__(self, corups: BaseGallery, num_rec, strategy: str) -> None:
        self.corups = corups
        self.num_rec = num_rec
        assert strategy in {
            "random",
            "popularity",
        }, f"Only support `random` and `popularity` strategies, while got {strategy}."
        self.strategy = strategy

    def run(self, chat_history: str):
        if self.strategy == "random":
            items = self.corups.corups.sample(self.num_rec, replace=False).to_dict(
                orient="list"
            )
            item_titles = items["title"]
        else:
            items = self.corups.corups.sample(
                self.num_rec, replace=False, weights="visited_num"
            ).to_dict(orient="list")
            item_titles = items["title"]
        return "; ".join(item_titles)


def hit_judge(msg: str, target: str, thres: float = 80):
    msg = re.sub(r"[^a-zA-Z0-9\s]", "", msg.lower())
    target = re.sub(r"[^a-zA-Z0-9\s]", "", target.lower())
    if fuzz.partial_ratio(msg, target) > thres:
        return True
    else:
        return False


def one_turn_conversation_eval(data: List[Dict], agent: CRSAgent):
    conversation = []
    hit_num = 0
    for i, d in enumerate(tqdm(data)):
        print("%"*100)
        print(d)
        print("%"*100)
        agent_msg = agent.run(d["context"])
        if hit_judge(agent_msg, d["target"]):
            hit_num += 1

        tqdm.write(f"Sample {i}: Hit={hit_num}/{i+1}={(hit_num/(i+1)):.4f}")
        conversation.append(
            {"context": d["context"], "target": d["target"], "answer": agent_msg}
        )

    # hit_turn = np.array(hit_turn)
    hit_ratio = hit_num / len(data)
    return {"hit": hit_ratio}, conversation


def main():
    parser = argparse.ArgumentParser("Evaluator")
    parser.add_argument(
        "--data", type=str, default="./data/steam/simulator_test_data.jsonl"
    )
    parser.add_argument(
        "--max_turns", type=int, default=5, help="max turns limit for evaluation"
    )
    parser.add_argument("--save", type=str, help="path to save conversation text")
    parser.add_argument(
        "--engine",
        type=str,
        default="text-davinci-003",
        help="Engine of OpenAI API to use as user simulator. The default is text-davinci-003",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=5,
        help="Timeout threshold when calling OAI. (seconds)",
    )

    # parser.add_argument('--domain', type=str, default='game')
    parser.add_argument(
        "--agent",
        type=str,
        help='agent type, "recbot" is our method and others are baselines',
    )
    parser.add_argument(
        "--num_rec", type=int, default=5, help="number of items to be recommended"
    )

    # recbot-agent
    parser.add_argument(
        "--max_candidate_num",
        type=int,
        default=1000,
        help="Number of max candidate number of buffer",
    )
    parser.add_argument(
        "--similar_ratio",
        type=float,
        default=0.1,
        help="Ratio of returned similar items / total games",
    )
    parser.add_argument(
        "--rank_num",
        type=int,
        default=100,
        help="Number of games given by ranking tool",
    )
    parser.add_argument(
        "--max_output_tokens",
        type=int,
        default=512,
        help="Max number of tokens in LLM output",
    )
    parser.add_argument(
        "--bot_type",
        type=str,
        default="completion",
        choices=["chat", "completion"],
        help="Type OpenAI models. The default is completion. Options [completion, chat]",
    )

    # chat history shortening
    parser.add_argument(
        "--enable_shorten",
        type=int,
        choices=[0, 1],
        default=0,
        help="Whether to enable shorten chat history with LLM",
    )

    # dynamic demonstrations
    parser.add_argument(
        "--demo_mode",
        type=str,
        choices=["zero", "fixed", "dynamic"],
        default="zero",
        help="Directory path of demonstrations",
    )
    parser.add_argument(
        "--demo_dir_or_file", type=str, help="Directory or file path of demonstrations"
    )
    parser.add_argument(
        "--num_demos",
        type=int,
        default=3,
        help="number of demos for in-context learning",
    )

    # reflection mechanism
    parser.add_argument(
        "--enable_reflection",
        type=int,
        choices=[0, 1],
        default=0,
        help="Whether to enable reflection",
    )
    parser.add_argument(
        "--reflection_limits", type=int, default=3, help="limits of reflection times"
    )

    # plan first agent
    parser.add_argument(
        "--plan_first",
        type=int,
        choices=[0, 1],
        default=0,
        help="Whether to use plan first agent",
    )

    parser.add_argument(
        "--langchain",
        type=int,
        choices=[0, 1],
        default=0,
        help="Whether to use langchain in plan-first agent",
    )

    args, _ = parser.parse_known_args()

    domain = os.environ.get("DOMAIN", "game")

    domain_map = {"item": domain, "Item": domain.capitalize(), "ITEM": domain.upper()}

    eval_data = read_jsonl(args.data)

    conversation = Conversation()

    if args.agent == "recbot":
        tool_names = {k: v.format(**domain_map) for k, v in TOOL_NAMES.items()}

        item_corups = BaseGallery(
            GAME_INFO_FILE,
            TABLE_COL_DESC_FILE,
            f"{domain}_information",
            columns=USE_COLS,
            fuzzy_cols=["title"] + CATEGORICAL_COLS,
            categorical_cols=CATEGORICAL_COLS,
        )

        candidate_buffer = CandidateBuffer(
            item_corups, num_limit=args.max_candidate_num
        )

        # The key of dict here is used to map to the prompt
        tools = {
            "BufferStoreTool": FuncToolWrapper(
                func=candidate_buffer.init_candidates,
                name=tool_names["BufferStoreTool"],
                desc=CANDIDATE_STORE_TOOL_DESC.format(**domain_map),
            ),
            "LookUpTool": QueryTool(
                name=tool_names["LookUpTool"],
                desc=LOOK_UP_TOOL_DESC.format(**domain_map),
                item_corups=item_corups,
                buffer=candidate_buffer,
            ),
            "HardFilterTool": SQLSearchTool(
                name=tool_names["HardFilterTool"],
                desc=HARD_FILTER_TOOL_DESC.format(**domain_map),
                item_corups=item_corups,
                buffer=candidate_buffer,
                max_candidates_num=args.max_candidate_num,
            ),
            "SoftFilterTool": SimilarItemTool(
                name=tool_names["SoftFilterTool"],
                desc=SOFT_FILTER_TOOL_DESC.format(**domain_map),
                item_sim_path=ITEM_SIM_FILE,
                item_corups=item_corups,
                buffer=candidate_buffer,
                top_ratio=args.similar_ratio,
            ),
            "RankingTool": RecModelTool(
                name=tool_names["RankingTool"],
                desc=RANKING_TOOL_DESC.format(**domain_map),
                model_fpath=MODEL_CKPT_FILE,
                item_corups=item_corups,
                buffer=candidate_buffer,
                rec_num=args.rank_num,
            ),
            "MapTool": MapTool(
                name=tool_names["MapTool"],
                desc=MAP_TOOL_DESC.format(**domain_map),
                item_corups=item_corups,
                buffer=candidate_buffer,
                max_rec_num=100,
            ),
            # "BufferClearTool": buffer_replan_tool
        }

        if args.enable_reflection:
            critic = Critic(
                model="gpt-4"
                if "4" in os.environ.get("AGENT_ENGINE", "")
                else "gpt-3.5-turbo",
                engine=os.environ.get("AGENT_ENGINE", ""),
                buffer=candidate_buffer,
                domain=domain,
            )
        else:
            critic = None

        if args.plan_first:
            if args.langchain:
                AgentType = CRSAgentPlanFirst
            else:
                AgentType = CRSAgentPlanFirstOpenAI
        else:
            AgentType = CRSAgent

        bot = AgentType(
            domain,
            tools,
            candidate_buffer,
            item_corups,
            os.environ.get("AGENT_ENGINE", ""),
            args.bot_type,
            max_tokens=args.max_output_tokens,
            enable_shorten=args.enable_shorten,  # history shortening
            demo_mode=args.demo_mode,
            demo_dir_or_file=args.demo_dir_or_file,
            num_demos=args.num_demos,  # demonstration
            critic=critic,
            reflection_limits=args.reflection_limits,
            reply_style="concise"
        )  # reflexion

        bot.init_agent()
        bot = RecBotWrapper(bot, args.num_rec)

    elif args.agent == "gpt4" or args.agent == "chatgpt":
        bot = OpenAIBot(
            domain=domain,
            engine=os.environ.get("AGENT_ENGINE"),
            api_base=os.environ.get("OPENAI_API_BASE"),
            api_key=os.environ.get("OPENAI_API_KEY"),
            api_version=os.environ.get("OPENAI_API_VERSION"),
            api_type=os.environ.get("OPENAI_API_TYPE"),
            num_rec=args.num_rec,
            timeout=args.timeout,
        )

    elif args.agent.startswith("llama") or args.agent.startswith(
        "vicuna"
    ):  # refer to fastchat to build API
        bot = OpenAIBot(
            domain=domain,
            engine="gpt-3.5-turbo",
            api_base=os.environ.get("OPENAI_API_BASE"),
            api_key="EMPTY",
            api_version=os.environ.get("OPENAI_API_VERSION"),
            api_type=os.environ.get("OPENAI_API_TYPE"),
            num_rec=args.num_rec,
            timeout=args.timeout,
            fschat=True,
        )

    elif args.agent in {"random", "popularity"}:
        corups = item_corups = BaseGallery(
            GAME_INFO_FILE,
            TABLE_COL_DESC_FILE,
            f"{domain}_information",
            columns=USE_COLS,
            fuzzy_cols=["title"] + CATEGORICAL_COLS,
            categorical_cols=CATEGORICAL_COLS,
        )

        bot = StaticAgent(corups, args.num_rec, strategy=args.agent)

    else:
        raise ValueError("Not support for such agent.")

    metrics, conversation = one_turn_conversation_eval(eval_data, bot)

    # save conversation
    current_time = datetime.now().strftime("%Y%m%d%H%M%S")
    if args.save is not None:
        save_path = args.save
    else:
        save_path = os.path.join(
            os.path.dirname(args.data),
            f"saved_conversations_{args.agent}_{current_time}_{os.path.basename(args.data)}",
        )
    write_jsonl(conversation, save_path)
    print(f"Conversation history saved in {save_path}.")

    print(metrics)


if __name__ == "__main__":
    main()
