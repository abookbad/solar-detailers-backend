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
    lastInitial: str
    streetAddress: str
    city: str
    phone: str | None = None
    pricePerPanel: str
    panelCount: str
    totalAmount: str

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

    v = vobject.vCard()
    
    # Name
    v.add('n')
    v.n.value = vobject.vcard.Name(family=p_info['last_name'], given=p_info['first_name'])
    v.add('fn')
    v.fn.value = f"{p_info['first_name']} {p_info['last_name']}"
    
    # Company
    v.add('org')
    v.org.value = ["Solar Detail"]

    # Phone
    phone_number = p_info.get("phone_number")
    if phone_number:
        v.add('tel')
        v.tel.value = p_info['phone_number']
        v.tel.type_param = 'CELL'
    
    # Address
    v.add('adr')
    v.adr.value = vobject.vcard.Address(street=p_info['address'])
    v.adr.type_param = 'HOME'

    # Notes
    price_per_panel = s_info['service_details'].split(' at $')[1].split(' per panel')[0]
    num_panels = s_info['service_details'].split(' panels')[0]
    total_quoted = s_info['quote_amount']
    note_content = (
        f"Price Per Panel: ${price_per_panel}\n"
        f"# of Panels: {num_panels}\n"
        f"$ Quoted: ${total_quoted:.2f}"
    )
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
        service_details = latest_service.get("service_details", "")
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

# --- Discord Bot Functions ---
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
        first_name = customer_data["personal_info"]["first_name"].lower()
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
        
        full_name = f"{p_info['first_name']} {p_info['last_name']}"
        phone_number = p_info['phone_number']
        full_address = p_info['address']
        
        # Generate Apple Maps link
        apple_maps_link = f"https://maps.apple.com/?q={quote_plus(full_address)}"

        price_per_panel = s_info['service_details'].split(' at $')[1].split(' per panel')[0]
        num_panels = s_info['service_details'].split(' panels')[0]
        total_quoted = s_info['quote_amount']

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

        # --- Message 3: Quote, Contacts Link ---
        message3_content = (
            f"**Price Per Panel**: ${price_per_panel} | **# of Panels**: {num_panels}\n"
            f"**Quoted**: ${total_quoted:.2f}\n\n"
            f"**Add to Contacts**: [Click to Download]({vcard_url})"
        )
        await new_channel.send(message3_content)

        # --- Message 4: Just the Maps Link ---
        await new_channel.send(f"**Apple Maps Link**: {apple_maps_link}")
        logger.info(f"Successfully posted details to channel #{channel_name}")

        # --- Message 5: Warning if no phone number ---
        if not phone_number:
            await new_channel.send(
                "⚠️ **Action Required**: This client was created without a phone number. "
                "A phone number is required to send quotes and gallery links. "
                "Please add one when available to sync with GoHighLevel."
            )

    except Exception as e:
        logger.error(f"An unexpected error occurred in create_customer_channel_and_post: {e}")
        logger.error(traceback.format_exc())

async def parse_datetime_from_string(when_string: str) -> datetime | None:
    """Uses OpenAI to parse a natural language string into a datetime object."""
    try:
        current_utc_time = datetime.utcnow().isoformat()
        
        prompt = (
            f"You are an expert datetime parser. Your task is to convert a user's natural language description of a time into a precise ISO 8601 datetime string.\n\n"
            f"The current UTC time is: {current_utc_time}.\n\n"
            f"The user's request is: \"{when_string}\"\n\n"
            "Based on the current time and the user's request, what is the exact future UTC datetime they are referring to?\n\n"
            "Important rules:\n"
            "1. If the user doesn't specify AM/PM, use your best judgment. For example, if it's 10 AM and they say \"at 2\", they likely mean 2 PM today. If it's 4 PM and they say \"at 2\", they likely mean 2 PM tomorrow.\n"
            "2. Assume \"next week\" means 7 days from now unless a specific day is mentioned (e.g., \"next Tuesday\").\n"
            "3. Your final output MUST be ONLY the ISO 8601 string and nothing else. For example: \"2024-07-16T15:30:00Z\". Do not add any explanation or other text."
        )

        client = openai.OpenAI(api_key=OPENAI_API_KEY)
        response = client.chat.completions.create(
            model="gpt-4-turbo",
            messages=[
                {"role": "system", "content": "You are a datetime parser."},
                {"role": "user", "content": prompt}
            ],
            temperature=0,
            max_tokens=50
        )
        
        parsed_string = response.choices[0].message.content.strip()
        
        # Clean up potential markdown backticks
        if parsed_string.startswith("`") and parsed_string.endswith("`"):
            parsed_string = parsed_string[1:-1]
            
        # Handle potential 'Z' for UTC
        if parsed_string.endswith('Z'):
            parsed_string = parsed_string[:-1] + '+00:00'

        return datetime.fromisoformat(parsed_string)
        
    except Exception as e:
        logger.error(f"Failed to parse datetime using OpenAI: {e}")
        return None

async def move_channel_back_job(channel_id: int, user_id: int):
    """Job function to move a channel back to the live category and notify the user."""
    channel = client.get_channel(channel_id)
    user = client.get_user(user_id)
    
    if not channel:
        logger.warning(f"Could not find channel {channel_id} to move it back.")
        return

    guild = channel.guild
    live_category = discord.utils.get(guild.categories, name=DISCORD_CATEGORY_NAME)

    if not live_category:
        logger.error(f"Could not find the live category '{DISCORD_CATEGORY_NAME}' to move channel back.")
        return

    try:
        await channel.edit(category=live_category, reason="Reminder time reached.")
        
        notification_message = f"Reminder for {user.mention if user else 'user'}! It's time to get back to this client."
        await channel.send(notification_message)
        
        # Clean up the reminder from the JSON file
        for customer_dir in os.listdir(CUSTOMER_DATA_DIR):
            customer_file = os.path.join(CUSTOMER_DATA_DIR, customer_dir, "customer_data.json")
            if os.path.exists(customer_file):
                try:
                    with open(customer_file, "r+") as f:
                        data = json.load(f)
                        if data.get("discord_channel_id") == channel_id:
                            if "getback_info" in data:
                                del data["getback_info"]
                                f.seek(0)
                                json.dump(data, f, indent=4)
                                f.truncate()
                            break
                except (IOError, json.JSONDecodeError):
                    continue

    except discord.Forbidden:
        logger.error(f"Missing permissions to move channel {channel_id}.")
    except discord.HTTPException as e:
        logger.error(f"Failed to move channel {channel_id}: {e}")

@tree.command(name="getback", description="Set a reminder to get back to this client.")
@app_commands.describe(when="When to be reminded (e.g., 'in 2 hours', 'next Tuesday at 3pm')")
async def getback(interaction: discord.Interaction, when: str):
    await interaction.response.defer(ephemeral=True, thinking=True)
    
    # 1. Find contact ID for this channel
    contact_id = None
    if os.path.exists(CUSTOMER_DATA_DIR):
        for customer_dir in os.listdir(CUSTOMER_DATA_DIR):
            customer_path = os.path.join(CUSTOMER_DATA_DIR, customer_dir)
            if os.path.isdir(customer_path) and os.path.exists(os.path.join(customer_path, "customer_data.json")):
                with open(os.path.join(customer_path, "customer_data.json"), "r") as f:
                    data = json.load(f)
                if data.get("discord_channel_id") == interaction.channel.id:
                    contact_id = data.get("client_id")
                    break
    
    if not contact_id:
        await interaction.followup.send("This command can only be used in a client channel.", ephemeral=True)
        return
        
    # 2. Parse the time string
    target_datetime = await parse_datetime_from_string(when)
    if not target_datetime:
        await interaction.followup.send("I couldn't understand that time. Please try a different format (e.g., 'in 2 hours', 'tomorrow at 3pm').", ephemeral=True)
        return
        
    # 3. Determine if channel needs to be moved
    guild = interaction.guild
    incubator_category = discord.utils.get(guild.categories, name=INCUBATOR_CATEGORY_NAME)
    
    if not incubator_category:
        await interaction.followup.send(f"The category '{INCUBATOR_CATEGORY_NAME}' was not found.", ephemeral=True)
        return

    now = datetime.now(target_datetime.tzinfo)
    end_of_week = now + timedelta(days=(6 - now.weekday()))
    end_of_week = end_of_week.replace(hour=23, minute=59, second=59)

    channel_to_move = interaction.channel
    response_message = ""
    
    if target_datetime > end_of_week:
        try:
            await channel_to_move.edit(category=incubator_category, reason=f"Incubating until {target_datetime.strftime('%Y-%m-%d')}")
            response_message = f"Client channel moved to **{INCUBATOR_CATEGORY_NAME}**."
        except discord.Forbidden:
            await interaction.followup.send("I don't have permissions to move this channel.", ephemeral=True)
            return
        except discord.HTTPException as e:
            await interaction.followup.send(f"Failed to move channel: {e}", ephemeral=True)
            return
    
    # 4. Schedule the reminder job
    job_id = f"getback_{contact_id}"
    scheduler.add_job(
        move_channel_back_job, 
        'date', 
        run_date=target_datetime, 
        args=[channel_to_move.id, interaction.user.id],
        id=job_id,
        replace_existing=True
    )
    
    # 5. Save reminder info to customer file
    customer_file = os.path.join(CUSTOMER_DATA_DIR, contact_id, "customer_data.json")
    with open(customer_file, "r+") as f:
        data = json.load(f)
        data['getback_info'] = {
            'user_id': interaction.user.id,
            'target_datetime_utc': target_datetime.isoformat(),
            'when_string': when
        }
        f.seek(0)
        json.dump(data, f, indent=4)
        f.truncate()

    # 6. Send confirmation
    formatted_time = f"<t:{int(target_datetime.timestamp())}:F>"
    final_message = f"✅ Reminder set for {formatted_time}. {response_message}"
    await interaction.followup.send(final_message, ephemeral=True)

# --- Membership API Endpoints ---
@app.get("/jobs")
async def get_jobs_list():
    """
    Returns a list of all jobs, sorted by most recent service date.
    This is for the 'New Membership' UI to select a job to upgrade.
    """
    jobs = get_all_jobs()
    return {"jobs": jobs}

@app.post("/memberships/upgrade")
async def upgrade_to_membership(upgrade_data: MembershipUpgrade):
    """
    Receives membership upgrade details and sends an SMS invite to the user.
    Also stores the quoted maintenance price and plan details.
    """
    # 1. Find the customer's data file using the contactId
    customer_file = os.path.join(CUSTOMER_DATA_DIR, upgrade_data.contactId, "customer_data.json")
    if not os.path.exists(customer_file):
        raise HTTPException(status_code=404, detail="Customer data not found for the provided contact ID.")

    try:
        with open(customer_file, "r") as f:
            customer_data = json.load(f)
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="Failed to read customer data file.")
    
    # 2. Update membership information
    customer_data["membership_info"] = {
        "quoted_price": upgrade_data.pricePerBasis,
        "plan_basis_months": upgrade_data.planBasis,
        "invite_sent_date": datetime.utcnow().isoformat(),
        "status": "invited"
    }
    
    # 3. Save the updated customer data
    try:
        with open(customer_file, "w") as f:
            json.dump(customer_data, f, indent=4)
    except IOError as e:
        raise HTTPException(status_code=500, detail=f"Failed to save membership data: {e}")
    
    # 4. Get the necessary info for the SMS
    p_info = customer_data.get("personal_info", {})
    first_name = p_info.get("first_name")
    phone_number = p_info.get("phone_number")

    if not all([first_name, phone_number]):
        raise HTTPException(status_code=400, detail="Customer data is missing first name or phone number.")

    # 5. Send the SMS invite
    success, message = await send_ghl_sms_invite(upgrade_data.contactId, first_name, phone_number)

    if not success:
        raise HTTPException(status_code=500, detail=message)
    
    return {
        "message": message,
        "membership_details": {
            "quoted_price": upgrade_data.pricePerBasis,
            "plan_basis_months": upgrade_data.planBasis,
            "invite_sent_date": customer_data["membership_info"]["invite_sent_date"]
        }
    }

@app.get("/membership/details")
async def get_membership_details(contactId: str):
    """
    Retrieves all stored details for a customer using their contact ID.
    Used by the frontend to pre-fill the membership profile page.
    """
    customer_file = os.path.join(CUSTOMER_DATA_DIR, contactId, "customer_data.json")
    if not os.path.exists(customer_file):
        raise HTTPException(status_code=404, detail="Customer data not found for the provided contact ID.")

    try:
        with open(customer_file, "r") as f:
            customer_data = json.load(f)
        
        return customer_data
        
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="Failed to read or parse customer data file.")

@app.post("/stripeCustomer")
async def update_stripe_customer(contactId: str, payload: StripeCustomerPayload):
    """
    Updates the Stripe customer ID for a given contact.
    """
    # Verify the contact_id in the payload matches the query parameter
    if payload.contact_id != contactId:
        raise HTTPException(status_code=400, detail="Contact ID in payload does not match query parameter.")
    
    customer_file = os.path.join(CUSTOMER_DATA_DIR, contactId, "customer_data.json")
    if not os.path.exists(customer_file):
        raise HTTPException(status_code=404, detail="Customer data not found for the provided contact ID.")

    try:
        with open(customer_file, "r") as f:
            customer_data = json.load(f)
        
        # Update the Stripe customer ID
        customer_data["stripe_customer_id"] = payload.stripe_customer_id
        
        with open(customer_file, "w") as f:
            json.dump(customer_data, f, indent=4)
        
        return {"message": f"Stripe customer ID updated successfully for contact {contactId}"}
        
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="Failed to read or parse customer data file.")
    except IOError as e:
        raise HTTPException(status_code=500, detail=f"Failed to write customer data: {e}")

@app.post("/membership/status")
async def update_membership_status(payload: MembershipStatusUpdate):
    """
    Updates the membership status for a given contact (active, cancelled, etc.).
    """
    customer_file = os.path.join(CUSTOMER_DATA_DIR, payload.contactId, "customer_data.json")
    if not os.path.exists(customer_file):
        raise HTTPException(status_code=404, detail="Customer data not found for the provided contact ID.")

    try:
        with open(customer_file, "r") as f:
            customer_data = json.load(f)
        
        # Update the membership status
        if "membership_info" not in customer_data:
            customer_data["membership_info"] = {
                "quoted_price": 0.0,
                "plan_basis_months": 0,
                "invite_sent_date": "",
                "status": "not_invited"
            }
        
        # Update individual fields if they are provided
        if payload.status is not None:
            customer_data["membership_info"]["status"] = payload.status
        
        if payload.payment_method is not None:
            customer_data["membership_info"]["payment_method"] = payload.payment_method
        
        if payload.plan_basis_months is not None:
            customer_data["membership_info"]["plan_basis_months"] = payload.plan_basis_months
        
        if payload.quoted_price is not None:
            customer_data["membership_info"]["quoted_price"] = payload.quoted_price
        
        # Add timestamp for status change
        customer_data["membership_info"]["status_updated_date"] = datetime.utcnow().isoformat()
        
        with open(customer_file, "w") as f:
            json.dump(customer_data, f, indent=4)
        
        response_data = {
            "message": f"Membership information updated for contact {payload.contactId}",
            "membership_info": customer_data["membership_info"]
        }
        
        return response_data
        
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="Failed to read or parse customer data file.")
    except IOError as e:
        raise HTTPException(status_code=500, detail=f"Failed to write customer data: {e}")

@app.get("/membership/status")
async def get_membership_status(contactId: str):
    """
    Retrieves the membership status for a given contact.
    """
    customer_file = os.path.join(CUSTOMER_DATA_DIR, contactId, "customer_data.json")
    if not os.path.exists(customer_file):
        raise HTTPException(status_code=404, detail="Customer data not found for the provided contact ID.")

    try:
        with open(customer_file, "r") as f:
            customer_data = json.load(f)
        
        # Get membership info or return default
        membership_info = customer_data.get("membership_info", {
            "quoted_price": 0.0,
            "plan_basis_months": 0,
            "invite_sent_date": "",
            "status": "not_invited"
        })
        
        response_data = {
            "contactId": contactId,
            "membership_info": membership_info
        }
        
        return response_data
        
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="Failed to read or parse customer data file.")
    except IOError as e:
        raise HTTPException(status_code=500, detail=f"Failed to read customer data: {e}")

# --- Calendar API Endpoints ---

@app.get("/appointments/{day}")
async def get_day_appointments(day: str):
    """
    Get all appointments for a specific day (YYYY-MM-DD).
    """
    try:
        target_date = date.fromisoformat(day)
        appointments = calendar_manager.get_appointments_for_day(target_date)
        return {"date": target_date, "appointments": appointments}
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format. Please use YYYY-MM-DD.")

@app.get("/appointments/available/{day}")
async def get_available_appointment_slots(day: str):
    """
    Get available 1-hour appointment slots for a specific day (YYYY-MM-DD).
    """
    try:
        target_date = date.fromisoformat(day)
        slots = calendar_manager.get_available_slots(target_date)
        return {"date": target_date, "available_slots": slots}
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format. Please use YYYY-MM-DD.")

@app.get("/appointments/available/bulk/{days_in_advance}")
async def get_bulk_available_slots_endpoint(days_in_advance: int):
    """
    Get available appointment slots for the next X days.
    Returns a dictionary with dates as keys and lists of available slots as values.
    """
    # Set a reasonable limit to prevent abuse
    if not 0 < days_in_advance <= 60:
        raise HTTPException(status_code=400, detail="Days in advance must be between 1 and 60.")
    
    slots = calendar_manager.get_bulk_available_slots(days_in_advance)
    return slots

@app.post("/appointments/book")
async def book_new_appointment(booking: AppointmentBooking):
    """
    Book a new appointment for a given contact ID and ISO timestamp.
    """
    success, message = calendar_manager.book_appointment(booking.contact_id, booking.start_time_iso)
    if not success:
        raise HTTPException(status_code=409, detail=message) # 409 Conflict
    return {"message": message}

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
                last_name=form_data.lastInitial,
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
            # Create the customer directory
            os.makedirs(customer_dir, exist_ok=True)
            logger.info(f"Created customer directory: {customer_dir}")

            # Create a customer_data.json file
            customer_data = {
                "client_id": contact_id, # Use the contact_id as the client_id
                "personal_info": {
                    "first_name": form_data.firstName,
                    "last_name": form_data.lastInitial, # Map lastInitial to last_name
                    "email": "",  # Email is missing in the new payload, saving as empty.
                    "phone_number": cleaned_phone,
                    "address": form_data.streetAddress, # Use combined address
                },
                "service_history": [
                    {
                        "service_date": datetime.utcnow().isoformat(),
                        "service_details": f"{form_data.pricePerPanel} at ${form_data.pricePerPanel} per panel",
                        "quote_amount": float(form_data.totalAmount),
                        "status": "completed"
                    }
                ],
                "membership_info": {
                    "quoted_price": 0.0,
                    "plan_basis_months": 0,
                    "invite_sent_date": "",
                    "status": "not_invited"
                },
                "discord_channel_id": None, # Will be set after channel creation
                "archived_in_thread_id": None # Will be set after channel archival
            }

            with open(os.path.join(customer_dir, "customer_data.json"), "w") as f:
                json.dump(customer_data, f, indent=4)
            logger.info(f"Created customer_data.json for {contact_id}")

            # Create a Discord channel for the customer
            await create_customer_channel_and_post(customer_data)
            logger.info(f"Created Discord channel for {contact_id}")

            return {
                "message": "Customer created successfully.",
                "contact_id": contact_id,
                "phone_number": cleaned_phone
            }

        except Exception as e:
            logger.error(f"Error creating customer {contact_id}: {e}")
            raise HTTPException(status_code=500, detail=f"Failed to create customer: {e}")

    except HTTPException as e:
        raise e
    except Exception as e:
        logger.error(f"An unexpected error occurred in create_customer: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to create customer: {e}")

@app.get("/")
def read_root():
    return {"message": "Welcome to the Solar Detailing Customer API"}

@app.get("/debug-token")
async def debug_token():
    """
    A temporary and secure endpoint to verify the GHL token being used by the application.
    """
    token = os.getenv("GHL_CONVERSATIONS_TOKEN")
    if token:
        # Show the first 8 and last 4 characters for verification, keeping the full token secret.
        token_preview = f"{token[:8]}...{token[-4:]}"
        return {
            "message": "This is a preview of the GHL Conversations Token the application is currently using.",
            "token_preview": token_preview
        }
    else:
        return {"error": "GHL_CONVERSATIONS_TOKEN is not set in the server's environment."}

@app.get("/api/images/{contact_id}/service_apt{service_num}")
async def get_service_images(contact_id: str, service_num: int):
    """
    Retrieves all before and after images for a specific service appointment.
    """
    # Check if customer exists
    customer_dir = os.path.join(CUSTOMER_DATA_DIR, contact_id)
    if not os.path.exists(customer_dir):
        raise HTTPException(status_code=404, detail=f"Customer not found: {contact_id}")
    
    # Log the access with the customer's name.
    customer_name = "Unknown"
    customer_file = os.path.join(customer_dir, "customer_data.json")
    if os.path.exists(customer_file):
        try:
            with open(customer_file, "r") as f:
                data = json.load(f)
            p_info = data.get("personal_info", {})
            customer_name = f"{p_info.get('first_name', '')} {p_info.get('last_name', '')}".strip()
        except (json.JSONDecodeError, IOError):
            logger.warning(f"Could not read customer data for {contact_id} to log gallery access.")
    
    logger.info(f"Service gallery accessed for {customer_name} (Contact ID: {contact_id}, Service: {service_num}).")
    
    # Check if service appointment exists
    service_dir = os.path.join(customer_dir, "images", f"service_apt{service_num}")
    if not os.path.exists(service_dir):
        # Let's check what service appointments are available
        images_dir = os.path.join(customer_dir, "images")
        available_services = []
        if os.path.exists(images_dir):
            available_services = [d for d in os.listdir(images_dir) if d.startswith("service_apt")]
        
        raise HTTPException(
            status_code=404, 
            detail=f"Service appointment {service_num} not found. Available services: {available_services}"
        )
    
    before_dir = os.path.join(service_dir, "before")
    after_dir = os.path.join(service_dir, "after")
    
    before_images = []
    after_images = []
    
    # Get before images
    if os.path.exists(before_dir):
        for filename in os.listdir(before_dir):
            if filename.lower().endswith(('.jpg', '.jpeg', '.png', '.gif', '.bmp')):
                image_url = f"{SERVER_BASE_URL}/images/{contact_id}/images/service_apt{service_num}/before/{filename}"
                before_images.append({
                    "filename": filename,
                    "url": image_url
                })
    
    # Get after images
    if os.path.exists(after_dir):
        for filename in os.listdir(after_dir):
            if filename.lower().endswith(('.jpg', '.jpeg', '.png', '.gif', '.bmp')):
                image_url = f"{SERVER_BASE_URL}/images/{contact_id}/images/service_apt{service_num}/after/{filename}"
                after_images.append({
                    "filename": filename,
                    "url": image_url
                })
    
    return {
        "contact_id": contact_id,
        "service_appointment": service_num,
        "before_images": before_images,
        "after_images": after_images,
        "total_images": len(before_images) + len(after_images)
    }

@app.get("/api/images/{contact_id}")
async def list_service_appointments(contact_id: str):
    """
    Lists all available service appointments with images for a contact.
    """
    # Check if customer exists
    customer_dir = os.path.join(CUSTOMER_DATA_DIR, contact_id)
    if not os.path.exists(customer_dir):
        raise HTTPException(status_code=404, detail=f"Customer not found: {contact_id}")
    
    images_dir = os.path.join(customer_dir, "images")
    if not os.path.exists(images_dir):
        return {
            "contact_id": contact_id,
            "service_appointments": [],
            "message": "No images directory found"
        }
    
    service_appointments = []
    
    for item in os.listdir(images_dir):
        if item.startswith("service_apt") and os.path.isdir(os.path.join(images_dir, item)):
            service_num = item.replace("service_apt", "")
            service_dir = os.path.join(images_dir, item)
            
            before_count = 0
            after_count = 0
            
            before_dir = os.path.join(service_dir, "before")
            after_dir = os.path.join(service_dir, "after")
            
            if os.path.exists(before_dir):
                before_count = len([f for f in os.listdir(before_dir) if f.lower().endswith(('.jpg', '.jpeg', '.png', '.gif', '.bmp'))])
            
            if os.path.exists(after_dir):
                after_count = len([f for f in os.listdir(after_dir) if f.lower().endswith(('.jpg', '.jpeg', '.png', '.gif', '.bmp'))])
            
            service_appointments.append({
                "service_number": int(service_num) if service_num.isdigit() else service_num,
                "before_image_count": before_count,
                "after_image_count": after_count,
                "total_images": before_count + after_count,
                "endpoint": f"/api/images/{contact_id}/service_apt{service_num}"
            })
    
    # Sort by service number
    service_appointments.sort(key=lambda x: x["service_number"] if isinstance(x["service_number"], int) else 999)
    
    return {
        "contact_id": contact_id,
        "service_appointments": service_appointments,
        "total_services": len(service_appointments)
    }

# --- Discord Bot Events and Commands ---
@client.event
async def on_ready():
    await tree.sync()
    logger.info(f'{client.user} has connected to Discord!')
    if scheduler.running:
        logger.info("/getback feature is enabled and scheduler is running.")

@client.event
async def on_message(message):
    # Don't respond to bot messages
    pass # This is a placeholder, as the bot is not configured to respond to messages yet.

if __name__ == "__main__":
    import uvicorn
    # This block is now only for local development.
    # The Uvicorn command should be used for running the combined app.
    config = uvicorn.Config(app, host="0.0.0.0", port=8000)
    server = uvicorn.Server(config)
    server.run() 