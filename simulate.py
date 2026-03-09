import asyncio
import random
from aiocoap import *

async def simulate():
    context = await Context.create_client_context()

    while True:
        for bed in range(1,101):

            temperature = round(random.uniform(36,41),1)
            bp = random.randint(100,180)
            heart_rate = random.randint(60,120)
            spo2 = random.randint(85,100)
            saline = random.randint(10,100)

            payload = (
                f"BED={bed},"
                f"TEMP={temperature},"
                f"BP={bp},"
                f"HR={heart_rate},"
                f"SPO2={spo2},"
                f"SALINE={saline}"
            )

            request = Message(
                code=POST,
                payload=payload.encode(),
                uri="coap://localhost/patient"
            )

            try:
                await context.request(request).response
            except Exception as e:
                print("Error:", e)

        await asyncio.sleep(25)

asyncio.run(simulate())
