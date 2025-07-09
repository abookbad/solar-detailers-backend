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

# Dashboard sync configuration
DASHBOARD_BASE_URL = os.getenv("DASHBOARD_BASE_URL", "http://your-dashboard-domain.com")
SERVER_BASE_URL = os.getenv("SERVER_BASE_URL", "http://windows.agencydevworks.ai:8000")

# --- Token Validation ---
if not all([GHL_API_TOKEN, GHL_CONVERSATIONS_TOKEN, BOT_TOKEN]):
    raise ValueError("One or more required environment variables are missing. Please check your .env file or server environment.")

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
    phone: str
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

# --- Helper Functions ---
def create_ghl_contact(first_name: str, last_name: str, phone: str, address: str, city: str) -> str | None:
    """Creates a contact in GHL and returns the contact ID."""
    
    # Clean and format the phone number
    cleaned_phone = re.sub(r'\\D', '', phone)
    if len(cleaned_phone) == 10 and not cleaned_phone.startswith('1'):
        cleaned_phone = '1' + cleaned_phone
    formatted_phone = f"+{cleaned_phone}"
    
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
    if not phone:
        return None

    # Clean the phone number to keep only digits
    cleaned_phone = re.sub(r'\D', '', phone)
    if not cleaned_phone:
        return None

    # For 10-digit numbers, assume US country code '1' if it's missing.
    if len(cleaned_phone) == 10 and not cleaned_phone.startswith('1'):
        cleaned_phone = '1' + cleaned_phone

    # Format the phone number for GHL API (e.g., +1909827...)
    formatted_phone = f"+{cleaned_phone}"

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
    
    # Format the phone number correctly
    cleaned_phone = re.sub(r'\D', '', to_number)
    if len(cleaned_phone) == 10 and not cleaned_phone.startswith('1'):
        cleaned_phone = '1' + cleaned_phone
    formatted_phone = f"+{cleaned_phone}"
    
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

async def upload_images_to_vercel(contact_id: str, image_files: list):
    """Uploads images to Vercel and returns the gallery URL."""
    # This is a placeholder for the Vercel upload logic
    # You'll need to implement the actual Vercel API call here
    
    # For now, return a placeholder URL
    # Replace this with actual Vercel upload logic
    gallery_url = f"https://your-vercel-app.vercel.app/gallery/{contact_id}"
    
    return gallery_url

async def send_gallery_link_to_client(contact_id: str, gallery_url: str, service_apt_num: int = 1):
    """Sends the gallery link to the client via SMS."""
    customer_file = os.path.join(CUSTOMER_DATA_DIR, contact_id, "customer_data.json")
    if not os.path.exists(customer_file):
        return False
    
    try:
        with open(customer_file, "r") as f:
            customer_data = json.load(f)
        
        p_info = customer_data.get("personal_info", {})
        first_name = p_info.get("first_name", "")
        phone_number = p_info.get("phone_number", "")
        
        if not phone_number:
            return False
        
        # Format the phone number correctly
        cleaned_phone = re.sub(r'\D', '', phone_number)
        if len(cleaned_phone) == 10 and not cleaned_phone.startswith('1'):
            cleaned_phone = '1' + cleaned_phone
        formatted_phone = f"+{cleaned_phone}"
        
        # Use the service gallery URL format
        service_gallery_url = f"https://your-domain.com/service-gallery/{contact_id}/{service_apt_num}"
        
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
        return True
        
    except Exception as e:
        return False

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
            f"**Phone Number**: {phone_number}\n"
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

    except Exception as e:
        logger.error(f"An unexpected error occurred in create_customer_channel_and_post: {e}")
        logger.error(traceback.format_exc())

# --- FastAPI Events ---
@app.on_event("startup")
async def startup_event():
    # Start the Discord bot in the background
    asyncio.create_task(client.start(BOT_TOKEN))

# --- Discord Bot Events and Commands ---
@client.event
async def on_ready():
    await tree.sync()

@client.event
async def on_message(message):
    # Don't respond to bot messages
    if message.author == client.user:
        return
    
    # Check if this channel has pending image uploads
    if hasattr(client, 'pending_uploads') and message.channel.id in client.pending_uploads:
        upload_info = client.pending_uploads[message.channel.id]
        
        # Check if the message has image attachments
        image_attachments = [att for att in message.attachments if att.content_type and att.content_type.startswith('image/')]
        
        if image_attachments:
            contact_id = upload_info['contact_id']
            image_type = upload_info['type']
            
            # Download and store the images
            await message.add_reaction('⏳')  # Processing reaction
            
            try:
                downloaded_files = await download_and_store_images(image_attachments, contact_id, image_type)
                
                if downloaded_files:
                    await message.add_reaction('✅')  # Success reaction
                    
                    service_apt_num = downloaded_files[0]['service_appointment'] if downloaded_files else 'Unknown'
                    success_msg = f"✅ Successfully saved {len(downloaded_files)} {image_type.upper()} image(s) for contact `{contact_id}` (Service Appointment #{service_apt_num})"
                    
                    # Store the uploaded files info for potential dashboard sync
                    if not hasattr(client, 'uploaded_images'):
                        client.uploaded_images = {}
                    
                    service_key = f"{contact_id}_service_{service_apt_num}"
                    if service_key not in client.uploaded_images:
                        client.uploaded_images[service_key] = {'before': [], 'after': []}
                    
                    client.uploaded_images[service_key][image_type] = downloaded_files
                    
                    # Check if we have both before and after images for this service
                    before_files = client.uploaded_images[service_key].get('before', [])
                    after_files = client.uploaded_images[service_key].get('after', [])
                    
                    # If it's "after" images, also upload to Vercel and send link to client
                    if image_type == 'after':
                        try:
                            gallery_url = await upload_images_to_vercel(contact_id, downloaded_files)
                            await send_gallery_link_to_client(contact_id, gallery_url, service_apt_num)
                            success_msg += f"\n📱 Gallery link sent to client: {gallery_url}"
                        except Exception as e:
                            success_msg += f"\n⚠️ Images saved locally, but failed to upload to Vercel: {e}"
                        
                        # Try to sync to dashboard if we have both before and after images
                        if before_files and after_files:
                            try:
                                sync_success, sync_result = await sync_service_to_dashboard(
                                    contact_id, service_apt_num, before_files, after_files
                                )
                                if sync_success:
                                    success_msg += f"\n🔄 Service synced to customer dashboard"
                                    # Clean up the stored images since sync is complete
                                    del client.uploaded_images[service_key]
                                else:
                                    success_msg += f"\n⚠️ Dashboard sync failed: {sync_result}"
                            except Exception as e:
                                success_msg += f"\n⚠️ Dashboard sync error: {e}"
                    
                    await message.reply(success_msg)
                else:
                    await message.add_reaction('❌')  # Error reaction
                    await message.reply("❌ No images were successfully downloaded.")
                
            except Exception as e:
                await message.add_reaction('❌')  # Error reaction
                await message.reply(f"❌ Error processing images: {e}")
            
            # Clear the pending upload for this channel
            del client.pending_uploads[message.channel.id]

@tree.command(name="hello", description="Says hello!")
async def hello(interaction: discord.Interaction):
    await interaction.response.send_message(f"Hello, {interaction.user.mention}!")

@tree.command(name="update", description="Upload before/after images for the client in this channel")
async def update_images(interaction: discord.Interaction):
    # Find the contact ID associated with this channel
    contact_id = None
    
    # Search through all customer directories to find which one has this channel ID
    if os.path.exists(CUSTOMER_DATA_DIR):
        for customer_dir in os.listdir(CUSTOMER_DATA_DIR):
            customer_path = os.path.join(CUSTOMER_DATA_DIR, customer_dir)
            if os.path.isdir(customer_path):
                customer_file = os.path.join(customer_path, "customer_data.json")
                if os.path.exists(customer_file):
                    try:
                        with open(customer_file, "r") as f:
                            data = json.load(f)
                        
                        # Check if this channel ID matches
                        if data.get("discord_channel_id") == interaction.channel.id:
                            contact_id = data.get("client_id")
                            break
                    except (json.JSONDecodeError, IOError):
                        continue
    
    if not contact_id:
        await interaction.response.send_message(
            "❌ This channel is not associated with any customer. "
            "The `/update` command can only be used in customer-specific channels created by the bot.",
            ephemeral=True
        )
        return
    
    # Create the view with Before/After buttons
    view = UpdateImageView(contact_id)
    
    # Get customer name for display
    customer_name = "Unknown"
    try:
        customer_file = os.path.join(CUSTOMER_DATA_DIR, contact_id, "customer_data.json")
        with open(customer_file, "r") as f:
            data = json.load(f)
        p_info = data.get("personal_info", {})
        customer_name = f"{p_info.get('first_name', '')} {p_info.get('last_name', '')}".strip()
    except:
        pass
    
    embed = discord.Embed(
        title="📸 Image Upload",
        description=f"**Customer:** {customer_name}\n**Contact ID:** `{contact_id}`\n\nChoose whether you want to upload **BEFORE** or **AFTER** images:",
        color=discord.Color.blue()
    )
    embed.add_field(
        name="📸 Before", 
        value="Upload images taken before the service", 
        inline=True
    )
    embed.add_field(
        name="✨ After", 
        value="Upload images taken after the service\n*(Will also send link to client)*", 
        inline=True
    )
    
    await interaction.response.send_message(embed=embed, view=view)

@tree.command(name="customers", description="Lists all customer IDs.")
async def list_customers(interaction: discord.Interaction):
    if not os.path.exists(CUSTOMER_DATA_DIR):
        await interaction.response.send_message("Customer data directory not found.")
        return
    customer_ids = [d for d in os.listdir(CUSTOMER_DATA_DIR) if os.path.isdir(os.path.join(CUSTOMER_DATA_DIR, d))]
    if not customer_ids:
        await interaction.response.send_message("No customers found.")
        return
    id_list = "\n".join([f"- `{cid}`" for cid in customer_ids])
    message = f"**Total Customers: {len(customer_ids)}**\n{id_list}"
    await interaction.response.send_message(message)

@tree.command(name="customer", description="Get details for a specific customer using their GHL Contact ID.")
@app_commands.describe(client_id="The GHL Contact ID of the client to look up")
async def get_customer(interaction: discord.Interaction, client_id: str):
    customer_dir = os.path.join(CUSTOMER_DATA_DIR, client_id)
    customer_file = os.path.join(customer_dir, "customer_data.json")
    if not os.path.exists(customer_file):
        await interaction.response.send_message(f"No data found for client ID: `{client_id}`")
        return
    try:
        with open(customer_file, "r") as f:
            data = json.load(f)
    except Exception as e:
        await interaction.response.send_message(f"Error reading customer data: {e}")
        return
    embed = discord.Embed(
        title=f"Customer Details: {data['personal_info']['first_name']} {data['personal_info']['last_name']}",
        color=discord.Color.blue()
    )
    embed.add_field(name="GHL Contact ID", value=f"`{data['client_id']}`", inline=False)
    embed.add_field(name="Email", value=data['personal_info']['email'], inline=True)
    embed.add_field(name="Phone", value=data['personal_info']['phone_number'], inline=True)
    embed.add_field(name="Address", value=data['personal_info']['address'], inline=False)
    if data.get("service_history"):
        embed.add_field(name="--- Last Service ---", value="", inline=False)
        last_service = data["service_history"][-1]
        embed.add_field(name="Date", value=last_service['service_date'], inline=True)
        embed.add_field(name="Quote", value=f"${last_service['quote_amount']:.2f}", inline=True)
        embed.add_field(name="Follow-up Date", value=last_service['follow_up_date'], inline=True)
        embed.add_field(name="Details", value=last_service['service_details'], inline=False)
    embed.set_footer(text=f"Data created on: {data['created_at']}")
    await interaction.response.send_message(embed=embed)

# --- API Endpoints ---
@app.post("/customer/create")
async def create_customer(payload: VercelWebhookPayload):
    """
    Webhook to create a GHL contact, then create a customer folder using the GHL ID.
    """
    logger.info(f"Received new customer payload: {payload.formData.model_dump_json()}")
    try:
        # Extract the actual form data from the nested object
        form_data = payload.formData

        # Create the contact in GHL first
        logger.info("Attempting to create GHL contact...")
        contact_id = create_ghl_contact(
            first_name=form_data.firstName,
            last_name=form_data.lastInitial,
            phone=form_data.phone,
            address=form_data.streetAddress,
            city=form_data.city
        )

        if not contact_id:
            logger.error("create_ghl_contact returned None. Aborting.")
            raise HTTPException(
                status_code=500, 
                detail="Failed to create contact in GoHighLevel. Contact ID was not returned."
            )

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
        service_details_text = f"{form_data.panelCount} panels at ${form_data.pricePerPanel} per panel."
        
        # Safely convert totalAmount to float, defaulting to 0.0 if empty or invalid.
        try:
            quote_amount = float(form_data.totalAmount)
        except (ValueError, TypeError):
            logger.warning(f"Could not convert totalAmount '{form_data.totalAmount}' to float. Defaulting to 0.0")
            quote_amount = 0.0

        customer_data = {
            "client_id": contact_id,  # GHL Contact ID is the main ID
            "personal_info": {
                "first_name": form_data.firstName,
                "last_name": form_data.lastInitial, # Map lastInitial to last_name
                "email": "",  # Email is missing in the new payload, saving as empty.
                "phone_number": form_data.phone,
                "address": full_address, # Use combined address
            },
            "service_history": [
                {
                    "service_date": service_date.isoformat(),
                    "quote_amount": quote_amount,
                    "service_details": service_details_text,
                    "follow_up_date": follow_up_date.isoformat(),
                }
            ],
            "membership_info": {
                "quoted_price": 0.0,
                "plan_basis_months": 0,
                "invite_sent_date": "",
                "status": "not_invited"  # not_invited, invited, active, cancelled
            },
            "stripe_customer_id": "",  # Initialize as empty, will be set later via /stripeCustomer endpoint
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

@app.post("/contactUpdate")
async def contact_update(contactId: str, payload: ContactUpdatePayload):
    """
    Receives an update for a contact, compares it to stored data,
    and sends a confirmation message to Discord if data is different.
    """
    customer_file = os.path.join(CUSTOMER_DATA_DIR, contactId, "customer_data.json")
    if not os.path.exists(customer_file):
        raise HTTPException(status_code=404, detail="Customer data not found.")

    with open(customer_file, "r") as f:
        existing_data = json.load(f)
    
    existing_p_info = existing_data.get("personal_info", {})
    
    # Create a dictionary from the payload, only including non-empty fields
    new_p_info = {
        "first_name": payload.firstName,
        "last_name": payload.lastName,
        "phone_number": existing_p_info.get("phone_number"), # Keep original phone
    }
    
    # Only add email and address if they were provided (non-empty)
    if payload.email:
        new_p_info["email"] = payload.email
    else:
        new_p_info["email"] = existing_p_info.get("email", "")
        
    if payload.address:
        new_p_info["address"] = payload.address
    else:
        new_p_info["address"] = existing_p_info.get("address", "")

    # Compare the dictionaries
    if existing_p_info == new_p_info:
        return {"message": "No changes detected. Data is already up-to-date."}

    # If different, send a message to the Discord channel
    channel_id = existing_data.get("discord_channel_id")
    if not channel_id:
        raise HTTPException(status_code=500, detail="Discord channel ID not found for this contact.")

    channel = client.get_channel(channel_id)
    if not channel:
        raise HTTPException(status_code=500, detail=f"Could not find Discord channel with ID: {channel_id}")

    # Format a comparison message
    diff_message = "A customer has updated their profile information. Please review the changes:\n\n"
    diff_message += "--- **Original Data** ---\n"
    for key, value in existing_p_info.items():
        diff_message += f"**{key.replace('_', ' ').title()}**: {value}\n"
    
    diff_message += "\n--- **New Data** ---\n"
    for key, value in new_p_info.items():
        diff_message += f"**{key.replace('_', ' ').title()}**: {value}\n"

    view = ConfirmUpdateView(contact_id=contactId, new_data=new_p_info)
    await channel.send(diff_message, view=view)

    return {"message": "Changes detected. A confirmation request has been sent to the staff channel."}

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

if __name__ == "__main__":
    import uvicorn
    # This block is now only for local development.
    # The Uvicorn command should be used for running the combined app.
    config = uvicorn.Config(app, host="0.0.0.0", port=8000)
    server = uvicorn.Server(config)
    server.run() 