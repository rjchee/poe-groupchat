"""

Bot that lets you have a conversation with multiple bots in Poe.

"""
from __future__ import annotations

from collections import defaultdict
import copy
import re
from typing import AsyncIterable, Optional

from fastapi_poe import PoeBot, run
from fastapi_poe.client import MetaMessage, stream_request
from fastapi_poe.types import QueryRequest
from sse_starlette.sse import ServerSentEvent


bot_id_pattern = re.compile("\[(?P<bot_id>[a-zA-Z0-9\-]{4,15})\]$")
# use a 3-letter name so it can't collide with a bot name
user_id = "Mod"
starting_prompt = f"You are in a group discussion moderated by {user_id}."
who_speaks_next_response = "(Which bot do you want to respond with next? Type their name in brackets, like [Sage])"


def identify_bot_id(message: str) -> Optional[str]:
    if (match := bot_id_pattern.match(message)) is not None:
        return match.group("bot_id")
    return None


class PopcornBot(PoeBot):
    async def get_response(self, query: QueryRequest) -> AsyncIterable[ServerSentEvent]:
        processed_request = []
        messages = []
        current_bot_id = None
        bots_in_convo = set()
        for message in query.query:
            content = message.content
            if message.role == "user":
                # strip out any bot identifiers from the message history
                if (bot_id := identify_bot_id(content)) is not None:
                    current_bot_id = bot_id
                    bots_in_convo.add(bot_id)
                else:
                    *maybe_content, maybe_bot_id = content.rstrip().rsplit("\n", maxsplit=1)
                    print(f"{maybe_content=}, {maybe_bot_id=}")
                    if (bot_id := identify_bot_id(maybe_bot_id)) is not None:
                        message = message.copy(update={"content": maybe_content[0]})
                        current_bot_id = bot_id
                        bots_in_convo.add(bot_id)
                    messages.append((user_id, message))
            else:
                if current_bot_id is None:
                    if content != who_speaks_next_response:
                        print(f"Warning! {current_bot_id=}, but {content=}, which is not the who speaks next response!")
                    continue
                messages.append((current_bot_id, message))
                current_bot_id = None
        if current_bot_id is None:
            yield self.text_event(who_speaks_next_response)
            for bot_id in sorted(bots_in_convo.union({"Sage", "Claude-instant"})):
                yield self.suggested_reply_event(f"[{bot_id}]")
            return

        # build query for the bot
        forwarded_messages = []
        previous_sender = None
        for sender, message in messages:
            if sender == current_bot_id:
                current_role = "bot"
                message_content = message.content
            else:
                current_role = "user"
                message_content = f"{sender}: {message.content}"
            if not forwarded_messages or forwarded_messages[-1].role != current_role:
                forwarded_messages.append(
                    message.copy(update={"role": current_role, "content": message_content})
                )
            else:
                forwarded_messages[-1] = forwarded_messages[-1].copy(
                    update={"content": f"{forwarded_messages[-1].content}\n\n{message_content}"}
                )
        assert forwarded_messages[0].role == "user", f"The first message in the conversation was not a user message: {forwarded_messages[0]}"
        forwarded_messages[0] = forwarded_messages[0].copy(
            update={"content": f"{starting_prompt}\n\n{forwarded_messages[0].content}"}
        )
        assert forwarded_messages[-1].role == "user", f"The last message in the conversation was not a user message: {forwarded_messages[-1]}"
        forwarded_messages[-1] = forwarded_messages[-1].copy(
            update={"content": f"{forwarded_messages[-1].content}\n\nNow it's your turn to speak. Remember to stay on topic with what {user_id} last said."}
        )
        forwarded_query = query.copy(update={"query": forwarded_messages})
        print(f"sending query to {current_bot_id}:\n{forwarded_query}")

        try:
            async for msg in stream_request(forwarded_query, current_bot_id, query.api_key):
                if isinstance(msg, MetaMessage):
                    continue
                elif msg.is_suggested_reply:
                    print(f"suggested reply event: {msg.text}")
                elif msg.is_replace_response:
                    yield self.replace_response_event(msg.text)
                else:
                    yield self.text_event(msg.text)
        except Exception as e:
            print(f"caught exception {e} while talking to {current_bot_id}")

        for bot_id in sorted(bots_in_convo.union({"Sage", "Claude-instant"})):
            yield self.suggested_reply_event(f"[{bot_id}]")


if __name__ == "__main__":
    run(PopcornBot())
