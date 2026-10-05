import asyncio
import json
import sys
from pathlib import Path

import websockets

HERE = Path(__file__).resolve().parent
# CQG's generated protobuf modules (WebAPI/, common/), kept apart from the samples repo's bundled google/ package.
sys.path.insert(0, str(HERE.parent / "vendor" / "cqg_proto"))

from WebAPI.webapi_2_pb2 import ClientMsg, ServerMsg
from WebAPI.user_session_2_pb2 import LogonResult

creds = json.loads((HERE / "credentials.json").read_text(encoding="utf-8"))


async def recv_server_msg(ws, timeout=15):
    msg = ServerMsg()
    msg.ParseFromString(await asyncio.wait_for(ws.recv(), timeout))
    return msg


async def main():
    async with websockets.connect(creds["server_url"]) as ws:
        client_msg = ClientMsg()
        logon = client_msg.logon
        logon.user_name = creds["user_name"]
        logon.password = creds["password"]
        logon.client_app_id = creds["client_app_id"]
        logon.client_version = creds["client_version"]
        logon.protocol_version_major = 2
        logon.protocol_version_minor = 230
        await ws.send(client_msg.SerializeToString())

        reply = await recv_server_msg(ws)
        result = reply.logon_result
        if result.result_code == LogonResult.ResultCode.RESULT_CODE_SUCCESS:
            print("LOGON OK")
            print(result)
            logoff = ClientMsg()
            logoff.logoff.text_message = "logon test done"
            await ws.send(logoff.SerializeToString())
        else:
            print(f"LOGON FAILED (code {result.result_code}): {result.text_message}")


if __name__ == "__main__":
    asyncio.run(main())
