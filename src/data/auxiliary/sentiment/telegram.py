import os
import csv
import asyncio
from datetime import datetime
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from telethon.tl.types import MessageEntityTextUrl, MessageMediaPhoto, MessageMediaDocument
from dotenv import load_dotenv

load_dotenv()

# -------------------------------------------------------------------
# CONFIGURATION – replace with your own values or set environment variables
# -------------------------------------------------------------------
API_ID = int(os.getenv('TG_API_ID'))
API_HASH = os.getenv('TG_API_HASH')
PHONE_NUMBER = os.getenv('TG_PHONE')

# Target channel/group – can be username (e.g., 'durov'), invite link, or phone number for a user
TARGET = 'hyperliquid_announcements'
DATE = datetime.now().strftime("%Y_%m_%d")
FILENAME = f'{TARGET}_{DATE}'
LIMIT = None

# -------------------------------------------------------------------

async def scrape_messages(client, entity, limit):
    """Scrape messages from the given entity and return a list of dicts."""
    messages = []
    async for message in client.iter_messages(entity, limit=limit):
        # Basic message data
        msg_data = {
            'id': message.id,
            'date': message.date.isoformat() if message.date else None,
            'sender_id': message.sender_id,
            'text': message.text or ''
        }

        # If it's a forwarded message
        if message.forward:
            msg_data['forward_from'] = getattr(message.forward, 'from_id', None)

        # Extract URLs from entities (if needed)
        urls = []
        if message.entities:
            for entity in message.entities:
                if isinstance(entity, MessageEntityTextUrl):
                    urls.append(entity.url)
        msg_data['urls'] = '; '.join(urls)

        messages.append(msg_data)
        print(f"Scraped message {message.id}")

    return messages

async def scrape_channel(channel: str = TARGET, limit: int = LIMIT, filename: str = FILENAME):
    # Create the client
    client = TelegramClient(f'{channel}_scrape_{str(limit)}', API_ID, API_HASH)

    try:
        await client.start(phone=PHONE_NUMBER)
        print("Client created and authenticated")

        # Resolve the target entity (channel/group/user)
        try:
            entity = await client.get_entity(channel)
        except Exception as e:
            print(f"Could not find entity '{channel}': {e}")
            return

        print(f"Scraping messages from: {getattr(entity, 'title')}")

        # Scrape messages
        messages = await scrape_messages(client, entity, limit)

        if not messages:
            print("No messages scraped.")
            return

        # Write to CSV
        keys = messages[0].keys()
        with open(f'./datasets/telegram_{filename}.csv', 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=keys)
            writer.writeheader()
            writer.writerows(messages)

        print(f"Scraped {len(messages)} messages and saved to {f'./datasets/telegram_{filename}.csv'}")

    except SessionPasswordNeededError:
        print("Two-factor authentication is enabled. Please enter your password.")
        # If you need to handle 2FA, you can implement it here.
    except Exception as e:
        print(f"An error occurred: {e}")
    finally:
        await client.disconnect()
        