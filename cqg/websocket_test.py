import asyncio
import websockets

# CQG Web API Demo WebSocket Endpoint
CQG_DEMO_URL ="wss://demoapi.cqg.com"

async def connect_to_cqg():
    print(f"Connecting to CQG Web API at {CQG_DEMO_URL}...")
    try:
        async with websockets.connect(CQG_DEMO_URL) as websocket:
            print("Connected to CQG Web API")
            while True:
                # Receive messages from the WebSocket
                message = await websocket.recv()
                print(f"Received message: {message}")

    except Exception as e:
        print(f"An error occurred: {e}")
if __name__ == "__main__":
    asyncio.get_event_loop().run_until_complete(connect_to_cqg())
