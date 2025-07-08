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
import asyncio
import discord
from discord import app_commands
from urllib.parse import quote_plus
import vobject
from dotenv import load_dotenv
import logging
import traceback

# --- LOGGING ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- CONFIGURATION ---
load_dotenv()

CUSTOMER_DATA_DIR = "customer_data"
STATS_DIR = "bot_data"
PAYMENTS_FILE = os.path.join(STATS_DIR, "payments.json")
GHL_API_TOKEN = os.getenv("GHL_API_TOKEN")
GHL_API_BASE_URL = "https://rest.gohighlevel.com/v1"
GHL_CONVERSATIONS_TOKEN = os.getenv("GHL_CONVERSATIONS_TOKEN")
GHL_LOCATION_ID = os.getenv("GHL_LOCATION_ID")
BOT_TOKEN = os.getenv("BOT_TOKEN")
DISCORD_GUILD_ID = int(os.getenv("DISCORD_GUILD_ID", 0))
DISCORD_CATEGORY_NAME = os.getenv("DISCORD_CATEGORY_NAME", "solar-installs")
SERVER_BASE_URL = os.getenv("SERVER_BASE_URL", "http://localhost:8000")
DASHBOARD_BASE_URL = os.getenv("DASHBOARD_BASE_URL")
OWNER_USER_ID = int(os.getenv("OWNER_USER_ID", "0"))

# --- VALIDATION ---
if not all([GHL_API_TOKEN, GHL_CONVERSATIONS_TOKEN, BOT_TOKEN, GHL_LOCATION_ID, DISCORD_GUILD_ID]):
    raise ValueError("One or more required environment variables are missing (including DISCORD_GUILD_ID).")

os.makedirs(CUSTOMER_DATA_DIR, exist_ok=True)
os.makedirs(STATS_DIR, exist_ok=True)
os.makedirs("static", exist_ok=True)

# --- FASTAPI & DISCORD SETUP ---
app = FastAPI()
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# --- MIDDLEWARE & STATIC FILES ---
app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/images", StaticFiles(directory=CUSTOMER_DATA_DIR), name="images")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # Allow all for now, lock down in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Pydantic Models (Matching API Docs) ---
class FormData(BaseModel):
    firstName: str
    lastInitial: str
    streetAddress: str
    city: str
    phone: str
    pricePerPanel: str
    panelCount: str
    totalAmount: str

class CustomerCreateRequest(BaseModel):
    formData: FormData

# --- HELPER FUNCTIONS ---
def create_ghl_contact(first_name: str, last_name: str, phone: str, address: str, city: str) -> str | None:
    """Creates a contact in GHL and returns the contact ID."""
    
    # Clean and format the phone number
    cleaned_phone = re.sub(r'\D', '', phone)
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
        "fromNumber": "+19094049641", # This should be configurable
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

async def upload_images_to_vercel(contact_id: str, image_files: list, service_apt_num: int):
    """Uploads images to Vercel and returns the gallery URL."""
    # This is a placeholder for the Vercel upload logic
    # You'll need to implement the actual Vercel API call here
    
    # For now, return a placeholder URL
    # Replace this with actual Vercel upload logic
    gallery_url = f"https://solardetailers.com/service-gallery/{contact_id}/{service_apt_num}"
    
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
            "fromNumber": "+19094049641", # This should be configurable
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
        response = requests.get(f"{DASHBOARD_BASE_URL}/sync-pictures?contactId={contact_id}")
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
        return False, "Contact not found in dashboard. Please ensure the customer has been added to the dashboard system first."
    
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
        response = requests.post(f"{DASHBOARD_BASE_URL}/sync-pictures", json=payload)
        
        if response.status_code == 200:
            result = response.json()
            return True, result
        else:
            error_msg = f"Dashboard sync failed: HTTP {response.status_code} - {response.text}"
            return False, error_msg
            
    except Exception as e:
        error_msg = f"Error syncing to dashboard: {e}"
        return False, error_msg

def update_total_earned(amount: float):
    """Reads, updates, and writes the total earned amount in a stats file."""
    os.makedirs(STATS_DIR, exist_ok=True)
    stats = {"total_earned": 0.0}
    if os.path.exists(PAYMENTS_FILE):
        try:
            with open(PAYMENTS_FILE, "r") as f:
                stats = json.load(f)
        except (json.JSONDecodeError, IOError):
            # If file is empty or corrupted, start with a fresh stats dict
            pass
    
    # Ensure total_earned is a float
    current_total = float(stats.get("total_earned", 0.0))
    stats["total_earned"] = current_total + amount
    
    try:
        with open(PAYMENTS_FILE, "w") as f:
            json.dump(stats, f, indent=4)
        return stats["total_earned"]
    except IOError:
        return None

def record_payment(contact_id: str, amount: float, channel_id: int):
    """
    Appends a payment record to the global payments.json file AND
    updates the specific customer's data file with their payment history.
    """
    # === 1. Update Global payments file ===
    global_payments = []
    if os.path.exists(PAYMENTS_FILE):
        try:
            with open(PAYMENTS_FILE, "r") as f:
                global_payments = json.load(f)
        except (json.JSONDecodeError, IOError):
            pass

    new_global_payment = {
        "contact_id": contact_id,
        "amount": amount,
        "date": datetime.utcnow().isoformat(),
        "channel_id": channel_id
    }
    global_payments.append(new_global_payment)

    try:
        with open(PAYMENTS_FILE, "w") as f:
            json.dump(global_payments, f, indent=4)
    except IOError as e:
        logger.error(f"Could not write to global payments file ({PAYMENTS_FILE}): {e}")
        return False, f"Could not write to global payments file: {e}"

    # === 2. Update Customer-specific payments file ===
    customer_file = os.path.join(CUSTOMER_DATA_DIR, contact_id, "customer_data.json")
    if not os.path.exists(customer_file):
        logger.error(f"Customer data file not found for {contact_id} when recording payment.")
        return False, "Customer data file not found."

    customer_total_paid = 0
    try:
        with open(customer_file, "r") as f:
            customer_data = json.load(f)

        if "payments" not in customer_data:
            customer_data["payments"] = []

        new_customer_payment = {
            "amount": amount,
            "date": datetime.utcnow().isoformat()
        }
        customer_data["payments"].append(new_customer_payment)
        
        customer_total_paid = sum(p['amount'] for p in customer_data["payments"])
        customer_data["total_paid"] = customer_total_paid

        with open(customer_file, "w") as f:
            json.dump(customer_data, f, indent=4)

    except (IOError, json.JSONDecodeError) as e:
        logger.error(f"Error updating customer file for {contact_id}: {e}")
        return False, f"Error updating customer file: {e}"

    # === 3. Return success and relevant totals ===
    global_total_earned = sum(p['amount'] for p in global_payments)

    result = {
        "global_total": global_total_earned,
        "customer_total": customer_total_paid
    }
    return True, result

def get_total_earned() -> float:
    """Calculates the total amount earned from the payments.json file."""
    if not os.path.exists(PAYMENTS_FILE):
        return 0.0
    
    try:
        with open(PAYMENTS_FILE, "r") as f:
            payments = json.load(f)
        
        total = sum(item.get('amount', 0) for item in payments)
        return total
    except (json.JSONDecodeError, IOError):
        return 0.0 # Return 0 if file is corrupt or unreadable

def create_customer_data_file(client_id: str, personal_info: dict, service_history: list) -> dict | None:
    """Creates a local JSON file to store customer data."""
    try:
        customer_dir = os.path.join(CUSTOMER_DATA_DIR, client_id)
        os.makedirs(customer_dir, exist_ok=True)
        customer_data = {
            "client_id": client_id,
            "personal_info": personal_info,
            "service_history": service_history,
            "payments": [],
            "total_paid": 0,
            "membership_info": {},
            "stripe_customer_id": None,
            "discord_channel_id": None,
            "created_at": datetime.utcnow().isoformat()
        }
        file_path = os.path.join(customer_dir, "customer_data.json")
        with open(file_path, "w") as f:
            json.dump(customer_data, f, indent=4)
        logger.info(f"Successfully created customer data file for {client_id}")
        return customer_data
    except IOError as e:
        logger.error(f"Failed to create customer data file for {client_id}: {e}")
        return None

async def create_discord_channel_for_customer(channel_name: str, contact_id: str, customer_data: dict) -> discord.TextChannel | None:
    guild = client.get_guild(DISCORD_GUILD_ID)
    if not guild:
        logger.error(f"Guild with ID '{DISCORD_GUILD_ID}' not found. Make sure DISCORD_GUILD_ID is correct and the bot is in the server.")
        return None
    
    category = discord.utils.get(guild.categories, name=DISCORD_CATEGORY_NAME)
    if not category:
        logger.warning(f"Category '{DISCORD_CATEGORY_NAME}' not found. Creating a new one.")
        try:
            category = await guild.create_category(DISCORD_CATEGORY_NAME)
        except discord.Forbidden:
            logger.error("Bot does not have permission to create categories.")
            return None

    try:
        channel = await guild.create_text_channel(
            name=channel_name,
            category=category,
            topic=f"Contact ID: {contact_id}"
        )
        
        customer_data["discord_channel_id"] = channel.id
        file_path = os.path.join(CUSTOMER_DATA_DIR, contact_id, "customer_data.json")
        with open(file_path, "w") as f:
            json.dump(customer_data, f, indent=4)

        # Create and post embed
        embed = discord.Embed(title=f"New Customer: {personal_info['first_name']} {personal_info['last_name']}", color=discord.Color.green())
        embed.add_field(name="Contact ID", value=f"`{contact_id}`", inline=False)
        embed.add_field(name="Phone", value=f"`{personal_info['phone_number']}`", inline=False)
        embed.add_field(name="Address", value=f"`{personal_info['address']}`", inline=False)
        embed.add_field(name="City", value=f"`{personal_info['city']}`", inline=False)
        embed.add_field(name="Email", value=f"`{personal_info['email']}`", inline=False)
        embed.add_field(name="Total Paid", value=f"${customer_data['total_paid']:.2f}", inline=False)
        embed.set_footer(text=f"Customer ID: {contact_id}")
        await channel.send(embed=embed)

        return channel
    except Exception as e:
        logger.error(f"Failed to create Discord channel: {e}")
        return None

# --- API ENDPOINTS ---
@app.post("/customer/create")
async def handle_customer_create(request: CustomerCreateRequest):
    form = request.formData
    last_name = form.lastInitial
    
    ghl_contact_id = create_ghl_contact(form.firstName, last_name, form.phone, form.streetAddress, form.city)
    if not ghl_contact_id:
        raise HTTPException(status_code=500, detail="Failed to create GHL contact.")

    service_details = f"{form.panelCount} panels at ${form.pricePerPanel} per panel."
    personal_info = { "first_name": form.firstName, "last_name": last_name, "phone_number": form.phone, "address": form.streetAddress, "city": form.city, "email": "" }
    service_history = [{ "service_date": datetime.utcnow().isoformat(), "service_details": service_details, "quote_amount": float(form.totalAmount), "follow_up_date": (datetime.utcnow() + timedelta(days=90)).isoformat(), "status": "pending" }]
    
    customer_data = create_customer_data_file(ghl_contact_id, personal_info, service_history)
    if not customer_data:
        raise HTTPException(status_code=500, detail="Failed to create local data file.")

    channel_name = f"{form.firstName}-{last_name}-{ghl_contact_id[-4:]}".lower().replace(" ", "-")
    await create_discord_channel_for_customer(channel_name, ghl_contact_id, customer_data)
        
    return {"message": "Customer folder created/updated successfully", "contact_id": ghl_contact_id}

@app.get("/jobs")
async def get_jobs():
    jobs = get_all_jobs() # Assume get_all_jobs is defined
    return {"jobs": jobs}

# --- DISCORD COMMANDS ---
# ... All /update, /paid, /sync, etc. commands go here ...

# --- BOT LIFECYCLE ---
@client.event
async def on_ready():
    logger.info(f"Logged in as {client.user}")
    guild = client.get_guild(DISCORD_GUILD_ID)
    if guild:
        tree.copy_global_to(guild=guild)
        await tree.sync(guild=guild)
        logger.info(f"Commands synced to guild: {guild.name}")
    else:
        logger.warning(f"Target guild with ID {DISCORD_GUILD_ID} not found. Commands not synced.")

# --- MAIN EXECUTION ---
if __name__ == "__main__":
    # Must run the bot in a separate thread/process
    # This setup is simplified. A better approach uses asyncio.gather or a process manager.
    import threading
    
    def run_bot():
        client.run(BOT_TOKEN)

    threading.Thread(target=run_bot).start()
    
    # Run FastAPI server
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

