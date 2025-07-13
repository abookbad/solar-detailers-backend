from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from datetime import datetime, timedelta, date
import uuid
import os
import json
import requests
import re
import time
import calendar_manager
import asyncio
import discord
from discord import app_commands
from urllib.parse import quote_plus
import vobject
from dotenv import load_dotenv
import logging
import traceback
import openai
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load environment variables from .env file for local development
load_dotenv()

# --- Configuration ---
CUSTOMER_DATA_DIR = "customer_data"
GHL_API_TOKEN = os.getenv("GHL_API_TOKEN")
GHL_API_BASE_URL = "https://rest.gohighlevel.com/v1"
GHL_SMS_FROM_NUMBER = "+19094049641"
GHL_CONVERSATIONS_TOKEN = os.getenv("GHL_CONVERSATIONS_TOKEN")
GHL_LOCATION_ID = "cWEwz6JBFHPY0LeC3ry3"
BOT_TOKEN = os.getenv("BOT_TOKEN")
DISCORD_CATEGORY_NAME = "Solar Detail"
INCUBATOR_CATEGORY_NAME = "Solar Detail Incubater"
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# Dashboard sync configuration
DASHBOARD_BASE_URL = os.getenv("DASHBOARD_BASE_URL", "http://your-dashboard-domain.com")
SERVER_BASE_URL = os.getenv("SERVER_BASE_URL", "http://windows.agencydevworks.ai:8000")

# --- Token Validation ---
if not all([GHL_API_TOKEN, GHL_CONVERSATIONS_TOKEN, BOT_TOKEN, OPENAI_API_KEY]):
    raise ValueError("One or more required environment variables are missing. Please check your .env file or server environment.")

# --- Scheduler Setup ---
jobstores = {
    'default': SQLAlchemyJobStore(url='sqlite:///jobs.sqlite')
}
scheduler = AsyncIOScheduler(jobstores=jobstores)

app = FastAPI()

# Mount the static directory to serve vCard files
os.makedirs("static", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

# Mount the customer_data directory to serve images
os.makedirs(CUSTOMER_DATA_DIR, exist_ok=True)
app.mount("/images", StaticFiles(directory=CUSTOMER_DATA_DIR), name="images")

# --- CORS Middleware ---
origins = [
    "http://localhost:3000",
    "http://localhost",
    "https://solardetailers.com",
    # You might want to add your Vercel deployment URL here as well
    # "https://your-vercel-app-name.vercel.app", 
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Discord Bot Setup ---
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# --- Pydantic Models ---
# Models updated to match the new webhook structure with a nested "formData" object.
class FormData(BaseModel):
    firstName: str
    lastName: str
    streetAddress: str
    city: str
    phone: str | None = None
    panelCount: int
    solarCleaning: bool
    pigeonMeshing: bool

class VercelWebhookPayload(BaseModel):
    formData: FormData
    # selectedDate and selectedTime are received but not used currently.

class AppointmentBooking(BaseModel):
    contact_id: str
    start_time_iso: str

class MembershipUpgrade(BaseModel):
    contactId: str
    planBasis: int  # e.g., 3, 6, 12 months
    pricePerBasis: float

class ContactUpdatePayload(BaseModel):
    # Define the fields the user can update from the frontend
    firstName: str
    lastName: str
    email: str = ""  # Make optional with default empty string
    # phone number is not included as it's the primary key for lookup
    address: str = ""  # Make optional with default empty string

class StripeCustomerPayload(BaseModel):
    stripe_customer_id: str
    contact_id: str

class MembershipStatusUpdate(BaseModel):
    contactId: str
    status: str = None  # "active", "cancelled", "invited"
    payment_method: str = None  # "cash", "stripe", etc.
    plan_basis_months: int = None
    quoted_price: float = None

# --- Discord UI Views (for Buttons) ---
class UpdateImageView(discord.ui.View):
    def __init__(self, contact_id: str):
        super().__init__(timeout=300)  # 5 minute timeout
        self.contact_id = contact_id

    @discord.ui.button(label="Before", style=discord.ButtonStyle.primary, emoji="📸")
    async def before_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            f"📸 **BEFORE Photos for Contact ID: `{self.contact_id}`**\n\n"
            "Please upload your BEFORE images by attaching them to your next message in this channel. "
            "You can upload multiple images at once.",
            ephemeral=True
        )
        # Set a flag to track what type of upload we're expecting
        # We'll store this in the channel's topic or use a simple dict
        if not hasattr(client, 'pending_uploads'):
            client.pending_uploads = {}
        client.pending_uploads[interaction.channel.id] = {
            'contact_id': self.contact_id,
            'type': 'before',
            'user_id': interaction.user.id
        }

    @discord.ui.button(label="After", style=discord.ButtonStyle.success, emoji="✨")
    async def after_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            f"✨ **AFTER Photos for Contact ID: `{self.contact_id}`**\n\n"
            "Please upload your AFTER images by attaching them to your next message in this channel. "
            "You can upload multiple images at once. After uploading, I'll process them and send the link to the client.",
            ephemeral=True
        )
        # Set a flag to track what type of upload we're expecting
        if not hasattr(client, 'pending_uploads'):
            client.pending_uploads = {}
        client.pending_uploads[interaction.channel.id] = {
            'contact_id': self.contact_id,
            'type': 'after',
            'user_id': interaction.user.id
        }

class ConfirmUpdateView(discord.ui.View):
    def __init__(self, contact_id: str, new_data: dict):
        super().__init__(timeout=86400)  # Timeout in seconds (24 hours)
        self.contact_id = contact_id
        self.new_data = new_data

    @discord.ui.button(label="Use New Information", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        customer_file = os.path.join(CUSTOMER_DATA_DIR, self.contact_id, "customer_data.json")
        try:
            with open(customer_file, "r") as f:
                current_data = json.load(f)
            
            # Update the personal info with the new data
            current_data["personal_info"] = self.new_data
            
            with open(customer_file, "w") as f:
                json.dump(current_data, f, indent=4)

            await interaction.response.send_message(f"✅ Contact `{self.contact_id}` has been **updated** with the new information by {interaction.user.mention}.", ephemeral=True)
            # Disable buttons after use
            for item in self.children:
                item.disabled = True
            await interaction.message.edit(view=self)

        except Exception as e:
            await interaction.response.send_message(f"❌ An error occurred while updating the data: {e}", ephemeral=True)

    @discord.ui.button(label="Keep Original", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(f"ℹ️ No changes were made to contact `{self.contact_id}`.", ephemeral=True)
        # Disable buttons after use
        for item in self.children:
            item.disabled = True
        await interaction.message.edit(view=self)

# --- API Endpoints ---

@app.get("/images/{contact_id}")
async def get_customer_images(contact_id: str):
    """
    Scans the directory for a given contact ID and returns a list of all
    publicly accessible image URLs.
    """
    contact_dir = os.path.join(CUSTOMER_DATA_DIR, contact_id, "images")
    if not os.path.isdir(contact_dir):
        return {"image_urls": []}

    image_urls = []
    for root, _, files in os.walk(contact_dir):
        for filename in files:
            if filename.lower().endswith(('.png', '.jpg', '.jpeg', '.gif')):
                # Construct the path relative to the CUSTOMER_DATA_DIR for the URL
                relative_path = os.path.relpath(os.path.join(root, filename), CUSTOMER_DATA_DIR)
                # Ensure forward slashes for the URL
                url_path = relative_path.replace(os.sep, '/')
                image_urls.append(f"{SERVER_BASE_URL}/images/{url_path}")
    
    return {"image_urls": image_urls}


class ConfirmDeleteChannelView(discord.ui.View):
    def __init__(self, contact_id: str):
        super().__init__(timeout=300)  # 5 minute timeout
        self.contact_id = contact_id

    @discord.ui.button(label="Delete Channel", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def delete_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)

        original_channel = interaction.channel
        channel_name = original_channel.name
        ARCHIVE_CHANNEL_ID = 1392404258338373703

        try:
            archive_channel = interaction.client.get_channel(ARCHIVE_CHANNEL_ID)
            if not archive_channel or not isinstance(archive_channel, discord.TextChannel):
                await interaction.followup.send(f"Archive channel with ID `{ARCHIVE_CHANNEL_ID}` not found or is not a text channel.", ephemeral=True)
                return

            transcript_parts = []
            current_part = (
                f"## Transcript for channel `#{channel_name}`\n"
                f"**Deleted by:** {interaction.user.mention} on {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}\n"
                f"**Channel ID:** `{original_channel.id}`\n---\n\n"
            )

            messages = [msg async for msg in original_channel.history(limit=None)]
            for message in reversed(messages):
                timestamp = message.created_at.strftime('%Y-%m-%d %H:%M:%S')
                author_display = message.author.name
                
                entry = f"**{author_display}** `({timestamp})`:\n{message.content or ' '}\n"

                if message.embeds:
                    for i, embed in enumerate(message.embeds):
                        entry += f"\n> `--- Embed Start ---`\n"
                        if embed.title: entry += f"> **{embed.title}**\n"
                        if embed.description: entry += f"> {embed.description.replace(chr(10), chr(10) + '> ')}\n"
                        for field in embed.fields:
                            entry += f"> **{field.name}**: {field.value.replace(chr(10), chr(10) + '> ')}\n"
                        entry += f"> `--- Embed End ---`\n"

                if message.attachments:
                    for att in message.attachments:
                        entry += f"📎 **Attachment:** `{att.filename}` - {att.url}\n"
                
                entry += "\n"

                if len(current_part) + len(entry) > 1900:
                    transcript_parts.append(current_part)
                    current_part = ""
                
                current_part += entry

            if current_part:
                transcript_parts.append(current_part)
            
            thread_name = f"{channel_name}-{datetime.now().strftime('%Y%m%d-%H%M')}"
            if len(thread_name) > 100:
                thread_name = thread_name[:100]

            thread = await archive_channel.create_thread(name=thread_name)
            
            for part in transcript_parts:
                await thread.send(part, allowed_mentions=discord.AllowedMentions.none())
            
            # Send the confirmation message BEFORE deleting the channel to avoid a race condition.
            await interaction.edit_original_response(content=f"Channel `#{channel_name}` has been successfully archived in {thread.mention}. Deleting original channel...")

            await original_channel.delete(reason=f"Archived to thread {thread.id} by {interaction.user.name}")
            
            data_file = os.path.join(CUSTOMER_DATA_DIR, self.contact_id, "customer_data.json")
            if os.path.exists(data_file):
                try:
                    with open(data_file, "r+") as f:
                        customer_data = json.load(f)
                        customer_data['archived_in_thread_id'] = thread.id
                        f.seek(0)
                        json.dump(customer_data, f, indent=4)
                        f.truncate()
                except Exception as e:
                    logger.error(f"Could not update customer file for {self.contact_id} with archive thread ID: {e}")

            await thread.send(f"✅ This is a complete archive of the deleted channel `#{channel_name}`.")
            # The final ephemeral message is sent before deletion, so no need for another one here.

        except discord.Forbidden:
            await interaction.edit_original_response(content=
                "❌ **Permission Error:** I lack permissions. I need to be able to `Read Message History`, `Manage Channels`, and `Create Threads`."
            )
        except discord.HTTPException as e:
            await interaction.edit_original_response(content=f"❌ **API Error:** {e}")
        except Exception as e:
            await interaction.edit_original_response(content=f"❌ **An unexpected error occurred:** {e}")
            logger.error(f"Error during channel archival/deletion: {e}\n{traceback.format_exc()}")

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.message.delete()
        await interaction.response.send_message("Channel deletion cancelled.", ephemeral=True, delete_after=5)

def clean_and_format_phone(phone: str) -> str:
    """
    Cleans and formats a phone number to E.164 format (+1XXXXXXXXXX).
    Handles US numbers that may or may not have a country code.
    Fixes common mistakes like a double '1' prefix.
    """
    if not phone:
        return ""
    
    # Keep only digits
    digits = re.sub(r'\D', '', phone)
    
    # Common case: 10 digits, no country code
    if len(digits) == 10:
        return f"+1{digits}"
    
    # Common case: 11 digits, starts with 1
    if len(digits) == 11 and digits.startswith('1'):
        return f"+{digits}"
    
    # Error case from user: 12 digits, starts with 11
    if len(digits) == 12 and digits.startswith('11'):
        return f"+{digits[1:]}"

    # For any other case, just add a + if it doesn't have one.
    if digits:
        return f"+{digits}"
        
    return ""

# --- Helper Functions ---
def create_ghl_contact(first_name: str, last_name: str, phone: str, address: str, city: str) -> str | None:
    """Creates a contact in GHL and returns the contact ID."""
    
    # Phone number should arrive pre-formatted from the endpoint.
    formatted_phone = phone
    
    headers = {
        "Authorization": f"Bearer {GHL_CONVERSATIONS_TOKEN}",
        "Version": "2021-07-28",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }
    
    payload = {
        "locationId": GHL_LOCATION_ID,
        "firstName": first_name,
        "lastName": last_name,
        "phone": formatted_phone,
        "address1": address,
        "city": city
    }
    
    try:
        response = requests.post("https://services.leadconnectorhq.com/contacts/", headers=headers, json=payload)
        response.raise_for_status()
        
        data = response.json()
        
        contact_id = data.get("contact", {}).get("id")
        if contact_id:
            logger.info(f"Successfully created GHL contact with ID: {contact_id}")
            return contact_id
        else:
            logger.error(f"GHL contact creation succeeded but no ID was returned. Response: {data}")
            return None
            
    except requests.exceptions.RequestException as e:
        error_details = "No response body"
        if hasattr(e, 'response') and e.response is not None:
            try:
                error_details = e.response.json()
            except json.JSONDecodeError:
                error_details = e.response.text
        logger.error(f"Failed to create GHL contact. Error: {e}, Details: {error_details}")
        return None

def get_ghl_contact_id(phone: str) -> str | None:
    """Looks up a contact in GHL by phone number and returns their ID."""
    formatted_phone = clean_and_format_phone(phone)
    if not formatted_phone:
        return None

    headers = {
        "Authorization": f"Bearer {GHL_API_TOKEN}",
        "Accept": "application/json"
    }
    params = {
        "phone": formatted_phone
    }
    
    try:
        response = requests.get(f"{GHL_API_BASE_URL}/contacts/lookup", headers=headers, params=params)
        response.raise_for_status()  # Raises an exception for bad status codes (4xx or 5xx)
        
        data = response.json()

        if data.get("contacts") and len(data["contacts"]) > 0:
            contact_id = data["contacts"][0].get("id")
            return contact_id
        else:
            return None
            
    except requests.exceptions.RequestException as e:
        return None

def get_all_jobs():
    """
    Scans the customer_data directory and returns a list of all jobs,
    sorted by the most recent service date.
    """
    all_jobs = []
    if not os.path.exists(CUSTOMER_DATA_DIR):
        return []

    for contact_id in os.listdir(CUSTOMER_DATA_DIR):
        customer_dir = os.path.join(CUSTOMER_DATA_DIR, contact_id)
        if os.path.isdir(customer_dir):
            customer_file = os.path.join(customer_dir, "customer_data.json")
            if os.path.exists(customer_file):
                try:
                    with open(customer_file, "r") as f:
                        data = json.load(f)
                    
                    p_info = data.get("personal_info", {})
                    s_history = data.get("service_history", [])
                    
                    if p_info and s_history:
                        job_data = {
                            "contactId": data.get("client_id"),
                            "fullName": f"{p_info.get('first_name', '')} {p_info.get('last_name', '')}".strip(),
                            "address": p_info.get("address"),
                            "phoneNumber": p_info.get("phone_number"),
                            "lastServiceDate": s_history[-1].get("service_date")
                        }
                        all_jobs.append(job_data)
                except (json.JSONDecodeError, IndexError):
                    continue
    
    all_jobs.sort(key=lambda x: x.get("lastServiceDate") or "", reverse=True)
    return all_jobs

def create_vcard_file(contact_id: str, customer_data: dict) -> str:
    """Creates a .vcf file for the customer and returns its URL."""
    p_info = customer_data["personal_info"]
    s_info = customer_data["service_history"][0]
    service_details = s_info.get("service_details", {})

    v = vobject.vCard()
    
    # Name
    v.add('n')
    v.n.value = vobject.vcard.Name(family=p_info.get('last_name', ''), given=p_info.get('first_name', ''))
    v.add('fn')
    v.fn.value = f"{p_info.get('first_name', '')} {p_info.get('last_name', '')}".strip()
    
    # Company
    v.add('org')
    v.org.value = ["Solar Detail"]

    # Phone
    phone_number = p_info.get("phone_number")
    if phone_number:
        v.add('tel')
        v.tel.value = phone_number
        v.tel.type_param = 'CELL'
    
    # Address
    v.add('adr')
    v.adr.value = vobject.vcard.Address(street=p_info.get('address', ''))
    v.adr.type_param = 'HOME'

    # Notes
    note_content = "Services:\n"
    if service_details.get("solar_cleaning"):
        note_content += "- Solar Panel Cleaning\n"
    if service_details.get("pigeon_meshing"):
        note_content += "- Pigeon Meshing\n"
    
    note_content += f"# of Panels: {service_details.get('panel_count', 'N/A')}\n"
    note_content += f"$ Quoted: ${s_info.get('quote_amount', 0.0):.2f}"
    
    v.add('note')
    v.note.value = note_content

    vcf_path = os.path.join("static", f"{contact_id}.vcf")
    with open(vcf_path, 'w') as f:
        f.write(v.serialize())

    # Return the public URL for the file
    return f"{SERVER_BASE_URL}/static/{contact_id}.vcf"

async def send_ghl_sms_invite(contact_id: str, first_name: str, to_number: str):
    """Sends the membership profile SMS invite via GHL."""
    
    formatted_phone = clean_and_format_phone(to_number)
    if not formatted_phone:
        return False, "Invalid or empty phone number provided."
    
    # Construct the unique profile link for the contact
    profile_link = f"https://solardetailers.com/membership/accept-invite?token={contact_id}"
    message = (
        f"Hey {first_name},\n"
        "Thanks for your interest in a solar maintence plan!\n"
        f"Here's a link to where you can create your profile : {profile_link}"
    )

    headers = {
        "Authorization": f"Bearer {GHL_CONVERSATIONS_TOKEN}",
        "Version": "2021-04-15",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }

    payload = {
        "type": "SMS",
        "contactId": contact_id,
        "fromNumber": GHL_SMS_FROM_NUMBER,
        "toNumber": formatted_phone,
        "message": message
    }

    try:
        response = requests.post("https://services.leadconnectorhq.com/conversations/messages", headers=headers, json=payload)
        response.raise_for_status()
        return True, "SMS invite sent successfully."
    except requests.exceptions.RequestException as e:
        # Try to get more error details from the response body if available
        error_details = e.response.json() if e.response else "No response body"
        return False, f"Failed to send SMS invite: {error_details}"

async def download_and_store_images(attachments, contact_id: str, image_type: str):
    """Downloads Discord attachments and stores them locally organized by service appointment."""
    import aiohttp
    
    # Get the current service appointment number for this contact
    customer_file = os.path.join(CUSTOMER_DATA_DIR, contact_id, "customer_data.json")
    if not os.path.exists(customer_file):
        raise Exception(f"Customer data not found for contact {contact_id}")
    
    try:
        with open(customer_file, "r") as f:
            customer_data = json.load(f)
        
        # Get the number of service appointments (this will be the current service number)
        service_history = customer_data.get("service_history", [])
        current_service_num = len(service_history)  # This gives us the current service appointment number
        
    except (json.JSONDecodeError, IOError) as e:
        raise Exception(f"Failed to read customer data: {e}")
    
    # Create directory structure: customer_data/{contact_id}/images/service_apt{num}/{before|after}/
    images_dir = os.path.join(CUSTOMER_DATA_DIR, contact_id, "images", f"service_apt{current_service_num}", image_type)
    os.makedirs(images_dir, exist_ok=True)
    
    downloaded_files = []
    
    async with aiohttp.ClientSession() as session:
        for i, attachment in enumerate(attachments):
            if attachment.content_type and attachment.content_type.startswith('image/'):
                try:
                    # Generate filename with timestamp and index
                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    file_extension = attachment.filename.split('.')[-1] if '.' in attachment.filename else 'jpg'
                    filename = f"{image_type}_{timestamp}_{i+1}.{file_extension}"
                    file_path = os.path.join(images_dir, filename)
                    
                    # Download the image
                    async with session.get(attachment.url) as resp:
                        if resp.status == 200:
                            with open(file_path, 'wb') as f:
                                f.write(await resp.read())
                            downloaded_files.append({
                                'filename': filename,
                                'path': file_path,
                                'original_name': attachment.filename,
                                'service_appointment': current_service_num
                            })
                        else:
                            pass
                except Exception as e:
                    pass
    
    return downloaded_files

async def send_gallery_link_to_client(contact_id: str, service_apt_num: int):
    """Sends the gallery link to the client via SMS."""
    customer_file = os.path.join(CUSTOMER_DATA_DIR, contact_id, "customer_data.json")
    if not os.path.exists(customer_file):
        return False, "Customer file not found."
    
    try:
        with open(customer_file, "r") as f:
            customer_data = json.load(f)
        
        p_info = customer_data.get("personal_info", {})
        first_name = p_info.get("first_name", "")
        phone_number = p_info.get("phone_number", "")
        
        if not phone_number:
            return False, "Phone number not found for contact."
        
        formatted_phone = clean_and_format_phone(phone_number)
        if not formatted_phone:
            return False, "Phone number is invalid or missing in customer file."
        
        # Use the correct service gallery URL format
        service_gallery_url = f"https://solardetailers.com/service-gallery/{contact_id}/{service_apt_num}"
        
        message = (
            f"Hi {first_name}! 📸\n\n"
            f"Your solar panel cleaning is complete! Check out the before and after photos here:\n"
            f"{service_gallery_url}\n\n"
            f"Thank you for choosing Solar Detail!"
        )

        headers = {
            "Authorization": f"Bearer {GHL_CONVERSATIONS_TOKEN}",
            "Version": "2021-04-15",
            "Content-Type": "application/json",
            "Accept": "application/json"
        }

        payload = {
            "type": "SMS",
            "contactId": contact_id,
            "fromNumber": GHL_SMS_FROM_NUMBER,
            "toNumber": formatted_phone,
            "message": message
        }

        response = requests.post("https://services.leadconnectorhq.com/conversations/messages", headers=headers, json=payload)
        response.raise_for_status()
        return True, service_gallery_url
        
    except requests.exceptions.RequestException as e:
        error_details = str(e)
        if hasattr(e, 'response') and e.response:
            try:
                error_details = e.response.json()
            except json.JSONDecodeError:
                error_details = e.response.text
        return False, f"Failed to send gallery link SMS: {error_details}"
    except Exception as e:
        return False, f"An unexpected error occurred in send_gallery_link_to_client: {e}"

async def check_contact_exists_in_dashboard(contact_id: str):
    """Check if contactId exists in the customer dashboard system."""
    try:
        response = requests.get(f"{DASHBOARD_BASE_URL}/api/backend/sync-pictures?contactId={contact_id}")
        if response.status_code == 200:
            data = response.json()
            return data.get('exists', False), data
        else:
            return False, None
    except Exception as e:
        return False, None

async def sync_service_to_dashboard(contact_id: str, service_apt_num: int, before_files: list, after_files: list):
    """Sync service appointment with before/after pictures to the customer dashboard."""
    
    # First check if contact exists in dashboard
    exists, contact_info = await check_contact_exists_in_dashboard(contact_id)
    if not exists:
        return False, "Contact not found in dashboard system"
    
    # Get customer data for service details
    customer_file = os.path.join(CUSTOMER_DATA_DIR, contact_id, "customer_data.json")
    try:
        with open(customer_file, "r") as f:
            customer_data = json.load(f)
        
        p_info = customer_data.get("personal_info", {})
        service_history = customer_data.get("service_history", [])
        
        # Get the latest service details (assuming it's the current one)
        latest_service = service_history[-1] if service_history else {}
        
        # Extract panel count from service details if available
        panels_count = 0
        service_details = latest_service.get("service_details", {})
        if "panels" in service_details:
            try:
                panels_count = int(service_details.split(" panels")[0].split()[-1])
            except:
                panels_count = 0
        
    except Exception as e:
        return False, f"Error reading customer data: {e}"
    
    # Build picture URLs
    before_pics = []
    after_pics = []
    
    # Convert local file paths to public URLs
    for file_info in before_files:
        # Convert path like: customer_data/contactId/images/service_apt1/before/filename.jpg
        # To URL like: http://windows.agencydevworks.ai:8000/images/contactId/service_apt1/before/filename.jpg
        relative_path = file_info['path'].replace('customer_data/', '').replace('\\', '/')
        public_url = f"{SERVER_BASE_URL}/images/{relative_path}"
        before_pics.append(public_url)
    
    for file_info in after_files:
        relative_path = file_info['path'].replace('customer_data/', '').replace('\\', '/')
        public_url = f"{SERVER_BASE_URL}/images/{relative_path}"
        after_pics.append(public_url)
    
    # Prepare sync payload
    payload = {
        "contactId": contact_id,
        "serviceType": "Solar Panel Cleaning Service",
        "beforePictures": before_pics,
        "afterPictures": after_pics,
        "panelsCount": panels_count,
        "technicianName": "Solar Detail Team",  # You can make this dynamic later
        "notes": f"Service appointment #{service_apt_num} completed. {service_details}"
    }
    
    try:
        response = requests.post(f"{DASHBOARD_BASE_URL}/api/backend/sync-pictures", json=payload)
        
        if response.status_code == 200:
            result = response.json()
            return True, result
        else:
            error_msg = f"Dashboard sync failed: HTTP {response.status_code} - {response.text}"
            return False, error_msg
            
    except Exception as e:
        error_msg = f"Error syncing to dashboard: {e}"
        return False, error_msg

async def create_customer_channel_and_post(customer_data: dict):
    try:
        # Find the guild (server) - assuming the bot is in only one server
        if not client.guilds:
            logger.error("Discord client is not connected to any guilds.")
            return

        guild = client.guilds[0]
        
        # Find the category
        category = discord.utils.get(guild.categories, name=DISCORD_CATEGORY_NAME)
        if not category:
            logger.error(f"Discord category '{DISCORD_CATEGORY_NAME}' not found.")
            return

        # Format channel name: #mm-dd-firstname
        first_name = customer_data["personal_info"].get("first_name", "new-client").lower()
        today = datetime.now().strftime("%m-%d")
        channel_name = f"{today}-{first_name}"

        # Create the new channel
        logger.info(f"Creating Discord channel: {channel_name}")
        new_channel = await guild.create_text_channel(channel_name, category=category)
        logger.info(f"Successfully created Discord channel with ID: {new_channel.id}")

        # --- IMPORTANT: Save the channel ID to the customer's file ---
        customer_data["discord_channel_id"] = new_channel.id
        customer_file_path = os.path.join(CUSTOMER_DATA_DIR, customer_data["client_id"], "customer_data.json")
        try:
            with open(customer_file_path, "w") as f:
                json.dump(customer_data, f, indent=4)
        except IOError as e:
            logger.error(f"Failed to save discord_channel_id to customer file. Error: {e}")
            # Continue anyway, but log the error

        # Prepare data for the message
        p_info = customer_data["personal_info"]
        s_info = customer_data["service_history"][0]
        service_details = s_info.get("service_details", {})
        
        full_name = f"{p_info.get('first_name', '')} {p_info.get('last_name', '')}".strip()
        phone_number = p_info.get('phone_number')
        full_address = p_info.get('address', 'N/A')
        
        # Generate Apple Maps link
        apple_maps_link = f"https://maps.apple.com/?q={quote_plus(full_address)}" if full_address != 'N/A' else "Not Available"

        # Generate the vCard and get its URL
        vcard_url = create_vcard_file(customer_data["client_id"], customer_data)

        # --- Message 1: Name, Phone, and Address Label ---
        message1_content = (
            f"**Name**: {full_name}\n"
            f"**Phone Number**: {phone_number or 'Not Provided'}\n"
            f"**Address**:"
        )
        await new_channel.send(message1_content)

        # --- Message 2: Just the Address ---
        await new_channel.send(full_address)

        # --- Message 3: Service Details ---
        services = []
        if service_details.get("solar_cleaning"):
            services.append("Solar Panel Cleaning")
        if service_details.get("pigeon_meshing"):
            services.append("Pigeon Meshing")
        
        services_text = ", ".join(services) if services else "No services specified"
        panel_count_text = f"**Panel Count**: {service_details.get('panel_count', 'N/A')}"

        message3_content = (
            f"**Services Booked**: {services_text}\n"
            f"{panel_count_text}\n\n"
            f"**Add to Contacts**: [Click to Download]({vcard_url})"
        )
        await new_channel.send(message3_content)

        # --- Message 4: Just the Maps Link ---
        await new_channel.send(f"**Apple Maps Link**: {apple_maps_link}")
        logger.info(f"Successfully posted details to channel #{channel_name}")

        # --- Message 5: Notification that user booked directly ---
        if service_details.get("solar_cleaning") or service_details.get("pigeon_meshing"):
            await new_channel.send(
                "ℹ️ This appointment was booked directly by the client through the website."
            )

        # --- Message 6: Warning if no phone number ---
        if not phone_number:
            await new_channel.send(
                "⚠️ **Action Required**: This client was created without a phone number. "
                "A phone number is required to send quotes and gallery links. "
                "Please add one when available to sync with GoHighLevel."
            )

    except Exception as e:
        logger.error(f"An unexpected error occurred in create_customer_channel_and_post: {e}")
        logger.error(traceback.format_exc())

@app.post("/customer/create")
async def create_customer(payload: VercelWebhookPayload):
    """
    Webhook to create a GHL contact, then create a customer folder using the GHL ID.
    If no phone is provided, a local record is created without GHL integration.
    """
    logger.info(f"Received new customer payload: {payload.formData.model_dump_json()}")
    try:
        # Extract the actual form data from the nested object
        form_data = payload.formData
        contact_id = None
        cleaned_phone = ""

        # Conditionally create GHL contact if a phone number is provided
        if form_data.phone:
            cleaned_phone = clean_and_format_phone(form_data.phone)
            if not cleaned_phone:
                raise HTTPException(status_code=400, detail="The provided phone number is invalid.")
            
            logger.info("Phone number provided. Attempting to create GHL contact...")
            contact_id = create_ghl_contact(
                first_name=form_data.firstName,
                last_name=form_data.lastName,
                phone=cleaned_phone,
                address=form_data.streetAddress,
                city=form_data.city
            )

            if not contact_id:
                logger.error("create_ghl_contact returned None. Aborting.")
                raise HTTPException(
                    status_code=500, 
                    detail="Failed to create contact in GoHighLevel. Contact ID was not returned."
                )
        else:
            # No phone number, so create a local-only contact with a new UUID.
            logger.info("No phone number provided. Creating a local contact with a new UUID.")
            contact_id = str(uuid.uuid4())

        # The GHL contact ID is now the primary identifier and folder name.
        customer_dir = os.path.join(CUSTOMER_DATA_DIR, contact_id)

        try:
            logger.info(f"Creating customer directory: {customer_dir}")
            # Use exist_ok=True in case we are updating an existing customer's service.
            os.makedirs(customer_dir, exist_ok=True)
        except OSError as e:
            logger.error(f"Failed to create customer directory: {customer_dir}. Error: {e}")
            raise HTTPException(status_code=500, detail=f"Failed to create customer directory: {e}")
        
        # Use current time for service date and add a follow-up date
        service_date = datetime.utcnow()
        follow_up_date = service_date + timedelta(days=90)
        
        # --- Map new form data to our customer data structure ---
        full_address = f"{form_data.streetAddress}, {form_data.city}"
        
        service_details_obj = {
            "solar_cleaning": form_data.solarCleaning,
            "pigeon_meshing": form_data.pigeonMeshing,
            "panel_count": form_data.panelCount,
        }

        customer_data = {
            "client_id": contact_id,  # GHL Contact ID is the main ID
            "personal_info": {
                "first_name": form_data.firstName,
                "last_name": form_data.lastName,
                "email": "",
                "phone_number": cleaned_phone,
                "address": full_address,
            },
            "service_history": [
                {
                    "service_date": service_date.isoformat(),
                    "quote_amount": 0.0, # Quote amount is not provided from this form
                    "service_details": service_details_obj,
                    "follow_up_date": follow_up_date.isoformat(),
                }
            ],
            "membership_info": {
                "quoted_price": 0.0,
                "plan_basis_months": 0,
                "invite_sent_date": "",
                "status": "not_invited"
            },
            "stripe_customer_id": "",
            "created_at": datetime.utcnow().isoformat(),
        }

        file_path = os.path.join(customer_dir, "customer_data.json")
        try:
            logger.info(f"Writing customer data to {file_path}")
            with open(file_path, "w") as f:
                json.dump(customer_data, f, indent=4)
        except IOError as e:
            logger.error(f"Failed to write customer data to {file_path}. Error: {e}")
            raise HTTPException(status_code=500, detail=f"Failed to write customer data: {e}")

        # Trigger the Discord bot to create the channel and post the message
        logger.info("Triggering Discord channel creation...")
        await create_customer_channel_and_post(customer_data)
        logger.info("Successfully completed customer creation process.")

        return {"message": "Customer folder created/updated successfully", "contact_id": contact_id}

    except Exception as e:
        logger.error(f"An unhandled exception occurred in /customer/create endpoint: {e}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail="An internal server error occurred.")

@app.on_event("startup")
async def startup_event():
    # Start the Discord bot in the background
    asyncio.create_task(client.start(BOT_TOKEN))
    # Start the scheduler
    scheduler.start()
