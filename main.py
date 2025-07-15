from fastapi import FastAPI, Request, HTTPException, Query
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
GHL_SMS_FROM_NUMBER = "+19093237655"
GHL_CONVERSATIONS_TOKEN = os.getenv("GHL_CONVERSATIONS_TOKEN")
GHL_LOCATION_ID = "cWEwz6JBFHPY0LeC3ry3"
BOT_TOKEN = os.getenv("BOT_TOKEN")
DISCORD_CATEGORY_NAME = "Solar Detail"
INCUBATOR_CATEGORY_NAME = "Solar Detail Incubater"
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# Dashboard sync configuration
DASHBOARD_BASE_URL = os.getenv("DASHBOARD_BASE_URL", "http://your-dashboard-domain.com")
SERVER_BASE_URL = os.getenv("SERVER_BASE_URL", "https://ssh.agencydevworks.ai:8000")

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
origins = ["*"]

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

@client.event
async def on_ready():
    """Event that runs when the bot is ready and connected to Discord."""
    logger.info(f"Logged in as {client.user} (ID: {client.user.id})")
    
    # Sync commands to a specific guild for instant updates.
    # This process first clears all commands from the guild and then adds the current ones back,
    # ensuring any stale commands like the old /update are removed.
    if client.guilds:
        guild = client.guilds[0]
        logger.info(f"Force-syncing commands for guild: {guild.name} ({guild.id}) to remove stale commands.")

        # 1. Clear all commands from the guild.
        tree.clear_commands(guild=guild)
        await tree.sync(guild=guild)

        # 2. Add the current commands from the code back to the guild and sync.
        tree.copy_global_to(guild=guild)
        await tree.sync(guild=guild)
        
        logger.info("Discord slash commands re-synced successfully to guild.")
    else:
        logger.warning("Bot is not in any guild. Skipping guild-specific command sync.")
        # Fallback to global sync if not in any guilds (will take longer to update)
        await tree.sync()
        logger.info("Discord slash commands synced globally.")


@client.event
async def on_message(message: discord.Message):
    """Processes messages to check for pending image uploads."""
    # Ignore messages from the bot itself
    if message.author == client.user:
        return

    # Check if we are waiting for an upload in this channel from the user who sent the message
    if not hasattr(client, 'pending_uploads'):
        client.pending_uploads = {}
    
    pending_upload = client.pending_uploads.get(message.channel.id)

    # Check if the message is from the correct user and has attachments
    if pending_upload and pending_upload['user_id'] == message.author.id and message.attachments:
        contact_id = pending_upload['contact_id']
        upload_type = pending_upload['type']
        
        # Acknowledge receipt and start processing
        processing_msg = await message.channel.send(f"⏳ Processing {len(message.attachments)} `{upload_type}` image(s)...")
        
        # Clear the pending upload state immediately to prevent double processing
        del client.pending_uploads[message.channel.id]
        
        try:
            downloaded_files = await download_and_store_images(message.attachments, contact_id, upload_type)

            if not downloaded_files:
                await processing_msg.edit(content="⚠️ No valid images were found in your message. Please try the command again.")
                return
            
            # Handle 'before' upload confirmation
            if upload_type == 'before':
                await processing_msg.edit(content=f"✅ Successfully saved {len(downloaded_files)} 'before' image(s) for client `{contact_id}`.")
            
            # Handle 'after' upload, confirmation, and SMS
            elif upload_type == 'after':
                service_apt_num = downloaded_files[0]['service_appointment']
                success, result_msg = await send_gallery_link_to_client(contact_id, service_apt_num)

                response_message = f"✅ Successfully saved {len(downloaded_files)} 'after' image(s) for `{contact_id}`.\n"
                if success:
                    response_message += f"✉️ Gallery link sent to client. View it here: {result_msg}"
                else:
                    response_message += f"⚠️ **Failed to send gallery link SMS**: {result_msg}"
                
                await processing_msg.edit(content=response_message)

                # Prompt to send a review link
                review_view = ReviewRequestView(contact_id=contact_id)
                await message.channel.send(
                    "Next, would you like to ask the client for a review?",
                    view=review_view
                )
        
        except Exception as e:
            logger.error(f"Error processing image upload: {e}\n{traceback.format_exc()}")
            await processing_msg.edit(content=f"❌ An error occurred during image processing: {e}")

class ReviewRequestView(discord.ui.View):
    def __init__(self, contact_id: str):
        super().__init__(timeout=600)  # 10 minute timeout
        self.contact_id = contact_id

    @discord.ui.button(label="Send Google Review Link", style=discord.ButtonStyle.success, emoji="⭐")
    async def send_review_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()

        button.disabled = True
        button.label = "Review Link Sent!"
        await interaction.message.edit(view=self)

        customer_file = os.path.join(CUSTOMER_DATA_DIR, self.contact_id, "customer_data.json")
        if not os.path.exists(customer_file):
            await interaction.followup.send("❌ Could not find customer data file.", ephemeral=True)
            return

        try:
            with open(customer_file, "r") as f:
                customer_data = json.load(f)
            
            p_info = customer_data.get("personal_info", {})
            first_name = p_info.get("first_name")
            phone_number = p_info.get("phone_number")

            if not all([first_name, phone_number]):
                await interaction.followup.send("❌ Client is missing a first name or phone number.", ephemeral=True)
                return
            
            success, message = await send_review_request_sms(contact_id=self.contact_id, first_name=first_name, to_number=phone_number)

            if success:
                await interaction.channel.send(f"✅ Review link sent to **{first_name}** by {interaction.user.mention}.")
            else:
                await interaction.followup.send(f"⚠️ Failed to send review link: {message}", ephemeral=True)

        except Exception as e:
            await interaction.followup.send(f"❌ An unexpected error occurred: {e}", ephemeral=True)

class UpdateValueModal(discord.ui.Modal):
    def __init__(self, contact_id: str, field_to_update: str):
        super().__init__(title=f"Update {field_to_update}")
        self.contact_id = contact_id
        self.field_to_update = field_to_update
        self.new_value_input = discord.ui.TextInput(
            label=f"New {field_to_update}",
            placeholder=f"Enter the new {field_to_update}...",
            style=discord.TextStyle.short,
            required=True
        )
        self.add_item(self.new_value_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        new_value = self.new_value_input.value
        customer_file = os.path.join(CUSTOMER_DATA_DIR, self.contact_id, "customer_data.json")

        try:
            with open(customer_file, "r+") as f:
                customer_data = json.load(f)
                
                # Update logic based on the field
                if self.field_to_update == "Name":
                    parts = new_value.split(" ", 1)
                    customer_data["personal_info"]["first_name"] = parts[0]
                    customer_data["personal_info"]["last_name"] = parts[1] if len(parts) > 1 else ""
                elif self.field_to_update == "Phone Number":
                    customer_data["personal_info"]["phone_number"] = clean_and_format_phone(new_value)
                elif self.field_to_update == "Price Per Panel":
                    customer_data["service_history"][-1]["service_details"]["price_per_panel"] = float(new_value)
                elif self.field_to_update == "# of Panels":
                    customer_data["service_history"][-1]["service_details"]["panel_count"] = int(new_value)
                elif self.field_to_update == "Quoted Price":
                    customer_data["service_history"][-1]["quote_amount"] = float(new_value)

                # Write changes back
                f.seek(0)
                json.dump(customer_data, f, indent=4)
                f.truncate()
            
            await interaction.followup.send(f"✅ Successfully updated `{self.field_to_update}` for contact `{self.contact_id}`.", ephemeral=True)
            await interaction.channel.send(f"ℹ️ **{interaction.user.mention} updated the following field:**\n- **{self.field_to_update}** was updated to `{new_value}`.")

        except (IOError, json.JSONDecodeError, ValueError, IndexError) as e:
            await interaction.followup.send(f"❌ An error occurred: {e}. Please ensure the value is in the correct format.", ephemeral=True)

class UpdateSelectView(discord.ui.View):
    def __init__(self, contact_id: str):
        super().__init__(timeout=180)
        self.contact_id = contact_id

    @discord.ui.select(
        placeholder="Choose a field to update...",
        options=[
            discord.SelectOption(label="Name", description="Update the client's full name."),
            discord.SelectOption(label="Phone Number", description="Update the client's phone number."),
            discord.SelectOption(label="Price Per Panel", description="Update the price per panel for the latest service."),
            discord.SelectOption(label="# of Panels", description="Update the panel count for the latest service."),
            discord.SelectOption(label="Quoted Price", description="Update the total quoted price for the latest service."),
        ],
        custom_id="update_select"
    )
    async def select_callback(self, interaction: discord.Interaction, select: discord.ui.Select):
        field_to_update = select.values[0]
        modal = UpdateValueModal(contact_id=self.contact_id, field_to_update=field_to_update)
        await interaction.response.send_modal(modal)

# --- Pydantic Models ---
# Models updated to match the new webhook structure with a nested "formData" object.
class FormData(BaseModel):
    firstName: str
    lastName: str | None = None
    lastInitial: str | None = None
    streetAddress: str
    city: str
    phone: str | None = None
    panelCount: int | None = None
    solarCleaning: bool | None = None
    pigeonMeshing: bool | None = None
    pricePerPanel: str | None = None
    totalAmount: str | None = None

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

class NewServicePayload(BaseModel):
    contactId: str
    pricePerPanel: str
    panelCount: str
    totalAmount: str

# --- Discord UI Views (for Buttons) ---
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

@tree.command(name="update", description="Update a client's information.")
async def update(interaction: discord.Interaction):
    """Starts an interactive process to update client data."""
    contact_id = _get_contact_id_from_channel(interaction.channel.id)
    if not contact_id:
        await interaction.response.send_message("❌ This command can only be used in a client's channel.", ephemeral=True)
        return
        
    view = UpdateSelectView(contact_id)
    await interaction.response.send_message("Please select the information you would like to update:", view=view, ephemeral=True)

@tree.command(name="review", description="Sends a Google review link to the client.")
async def review(interaction: discord.Interaction):
    """Sends a review link to the client associated with the current channel."""
    await interaction.response.defer(ephemeral=True, thinking=True)

    contact_id = _get_contact_id_from_channel(interaction.channel.id)
    if not contact_id:
        await interaction.followup.send("❌ This command can only be used in a client's dedicated channel.", ephemeral=True)
        return

    customer_file = os.path.join(CUSTOMER_DATA_DIR, contact_id, "customer_data.json")
    if not os.path.exists(customer_file):
        await interaction.followup.send(f"❌ Customer data file not found for contact ID: `{contact_id}`.", ephemeral=True)
        return

    try:
        with open(customer_file, "r") as f:
            customer_data = json.load(f)

        p_info = customer_data.get("personal_info", {})
        first_name = p_info.get("first_name")
        phone_number = p_info.get("phone_number")

        if not all([first_name, phone_number]):
            await interaction.followup.send("❌ This client is missing a first name or phone number in their file.", ephemeral=True)
            return

        success, message = await send_review_request_sms(contact_id, first_name, phone_number)

        if success:
            await interaction.followup.send(f"✅ Successfully sent a review link to {first_name}.", ephemeral=True)
            await interaction.channel.send(f"✉️ A Google review link has been sent to the client by {interaction.user.mention}.")
        else:
            await interaction.followup.send(f"⚠️ Failed to send review link: {message}", ephemeral=True)

    except (IOError, json.JSONDecodeError) as e:
        logger.error(f"Error handling /review command for {contact_id}: {e}")
        await interaction.followup.send("❌ An error occurred while reading the client's data file.", ephemeral=True)

@tree.command(name="paid", description="Logs a payment for the client and shows updated revenue totals.")
@app_commands.describe(amount="The payment amount received.")
async def paid(interaction: discord.Interaction, amount: float):
    """Logs a payment and displays revenue stats."""
    await interaction.response.defer(ephemeral=False, thinking=True)

    contact_id = _get_contact_id_from_channel(interaction.channel.id)
    if not contact_id:
        await interaction.followup.send("❌ This command can only be used in a client's dedicated channel.", ephemeral=True)
        return

    payments_file = os.path.join("bot_data", "payments.json")

    # --- Load existing payments ---
    try:
        if os.path.exists(payments_file) and os.path.getsize(payments_file) > 0:
            with open(payments_file, "r") as f:
                payments_data = json.load(f)
        else:
            payments_data = []
    except (IOError, json.JSONDecodeError):
        payments_data = []

    # --- Add new payment ---
    new_payment = {
        "contact_id": contact_id,
        "amount": amount,
        "channel_id": interaction.channel.id,
        "date": datetime.utcnow().isoformat()
    }
    payments_data.append(new_payment)

    # --- Save updated payments ---
    try:
        with open(payments_file, "w") as f:
            json.dump(payments_data, f, indent=4)
    except IOError as e:
        logger.error(f"Failed to write to payments.json: {e}")
        await interaction.followup.send("❌ An error occurred while saving the payment record.", ephemeral=True)
        return

    # --- Calculate and display stats ---
    stats = get_dashboard_stats()

    response_message = (
        f"✅ **Payment Logged!**\n\n"
        f"Amount: `${amount:,.2f}`\n"
        f"Client ID: `{contact_id}`\n\n"
        f"--- **Revenue Update** ---\n"
        f"Today: `${stats['dailyRevenue']:,.2f}`\n"
        f"This Week: `${stats['weeklyRevenue']:,.2f}`\n"
        f"This Month: `${stats['monthlyRevenue']:,.2f}`\n"
        f"**Total Revenue:** **`${stats['totalRevenue']:,.2f}`**"
    )

    await interaction.followup.send(response_message)

@tree.command(name="before", description="Initiates the process for uploading 'before' service pictures.")
async def before(interaction: discord.Interaction):
    """Asks the user to upload 'before' pictures for the client of the current channel."""
    contact_id = _get_contact_id_from_channel(interaction.channel.id)
    
    if not contact_id:
        await interaction.response.send_message(
            "❌ This command can only be used in a client's dedicated channel.",
            ephemeral=True
        )
        return

    await interaction.response.send_message(
        "📸 **Please upload your BEFORE images** by attaching them to your next message in this channel. You can upload multiple images at once.",
        ephemeral=True
    )
    
    if not hasattr(client, 'pending_uploads'):
        client.pending_uploads = {}
    
    client.pending_uploads[interaction.channel.id] = {
        'contact_id': contact_id,
        'type': 'before',
        'user_id': interaction.user.id
    }

@tree.command(name="after", description="Initiates the 'after' picture upload and sends the gallery link.")
async def after(interaction: discord.Interaction):
    """Asks the user to upload 'after' pictures, then sends the gallery link to the client."""
    contact_id = _get_contact_id_from_channel(interaction.channel.id)

    if not contact_id:
        await interaction.response.send_message(
            "❌ This command can only be used in a client's dedicated channel.",
            ephemeral=True
        )
        return
        
    await interaction.response.send_message(
        "✨ **Please upload your AFTER images** by attaching them to your next message in this channel. I will send the gallery link when you're done.",
        ephemeral=True
    )

    if not hasattr(client, 'pending_uploads'):
        client.pending_uploads = {}
        
    client.pending_uploads[interaction.channel.id] = {
        'contact_id': contact_id,
        'type': 'after',
        'user_id': interaction.user.id
    }

# --- API Endpoints ---

def _get_contact_id_from_channel(channel_id: int) -> str | None:
    """Finds the contact ID associated with a given Discord channel ID."""
    if not os.path.exists(CUSTOMER_DATA_DIR):
        return None
        
    for customer_dir in os.listdir(CUSTOMER_DATA_DIR):
        customer_path = os.path.join(CUSTOMER_DATA_DIR, customer_dir)
        if os.path.isdir(customer_path):
            customer_file = os.path.join(customer_path, "customer_data.json")
            if os.path.exists(customer_file):
                try:
                    with open(customer_file, "r") as f:
                        data = json.load(f)
                    if data.get("discord_channel_id") == channel_id:
                        return data.get("client_id")
                except (json.JSONDecodeError, IOError):
                    continue
    return None

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

def format_phone_for_display(phone: str) -> str:
    """Formats an E.164 number to (XXX) XXX-XXXX for display."""
    if not phone:
        return "Not Provided"
    
    # Remove non-digit characters
    digits = re.sub(r'\D', '', phone)
    
    # Handle numbers with country code (e.g., +19091234567)
    if len(digits) == 11 and digits.startswith('1'):
        area_code = digits[1:4]
        prefix = digits[4:7]
        line_number = digits[7:11]
        return f"({area_code}) {prefix}-{line_number}"
    
    # Handle numbers without country code (e.g., 9091234567)
    if len(digits) == 10:
        area_code = digits[0:3]
        prefix = digits[3:6]
        line_number = digits[6:10]
        return f"({area_code}) {prefix}-{line_number}"

    # Fallback for any other format
    return phone

# --- Helper Functions ---
def update_ghl_contact(contact_id: str, first_name: str, last_name: str, phone: str, address: str, city: str) -> bool:
    """Updates an existing contact in GHL using their contact ID."""
    
    headers = {
        "Authorization": f"Bearer {GHL_API_TOKEN}",
        "Version": "2021-07-28",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }
    
    payload = {
        "firstName": first_name,
        "lastName": last_name,
        "phone": phone,
        "address1": address,
        "city": city,
        "source": "public api"
    }
    
    update_url = f"{GHL_API_BASE_URL}/contacts/{contact_id}"
    
    try:
        response = requests.put(update_url, headers=headers, json=payload)
        response.raise_for_status()
        logger.info(f"Successfully updated GHL contact with ID: {contact_id}")
        return True
    except requests.exceptions.RequestException as e:
        error_details = "No response body"
        if hasattr(e, 'response') and e.response is not None:
            try:
                error_details = e.response.json()
            except json.JSONDecodeError:
                error_details = e.response.text
        logger.error(f"Failed to update GHL contact {contact_id}. Error: {e}, Details: {error_details}")
        return False

def create_ghl_contact(first_name: str, last_name: str, phone: str, address: str, city: str) -> tuple[str | None, bool]:
    """
    Creates a contact in GHL.
    Returns a tuple of (contact_id, is_new).
    If a duplicate is found, it returns the existing contact_id and is_new=False.
    """
    
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
            return contact_id, True
        else:
            logger.error(f"GHL contact creation succeeded but no ID was returned. Response: {data}")
            return None, False
            
    except requests.exceptions.RequestException as e:
        error_details = "No response body"
        if hasattr(e, 'response') and e.response is not None:
            try:
                error_details = e.response.json()
                if e.response.status_code == 400 and 'This location does not allow duplicated contacts' in error_details.get('message', ''):
                    contact_id = error_details.get('meta', {}).get('contactId')
                    if contact_id:
                        logger.info(f"Duplicate contact detected. Found existing GHL contact with ID: {contact_id}")
                        return contact_id, False # It's an existing contact
            except json.JSONDecodeError:
                error_details = e.response.text
        logger.error(f"Failed to create GHL contact. Error: {e}, Details: {error_details}")
        return None, False

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

async def get_customer_images(contact_id: str):
    """
    Scans the directory for a given contact ID and returns a list of all
    publicly accessible image URLs.
    """
    logger.info(f"Attempting to get images for contact_id: {contact_id}")
    
    # Construct the absolute path for robustness
    contact_dir = os.path.abspath(os.path.join(CUSTOMER_DATA_DIR, contact_id, "images"))
    logger.info(f"Checking for image directory at absolute path: {contact_dir}")

    if not os.path.isdir(contact_dir):
        logger.warning(f"Directory not found for contact {contact_id} at path: {contact_dir}")
        raise HTTPException(status_code=404, detail=f"Image directory not found for contact {contact_id}")

    logger.info(f"Directory found. Scanning for images in: {contact_dir}")
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

def get_all_jobs():
    """
    Scans the customer_data directory and returns a list of all jobs,
    sorted by the most recent service date.
    """
    all_jobs = []
    if not os.path.exists(CUSTOMER_DATA_DIR):
        return {"jobs": []}

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
    return {"jobs": all_jobs}

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

async def send_review_request_sms(contact_id: str, first_name: str, to_number: str):
    """Sends the Google review link request via GHL SMS."""
    
    formatted_phone = clean_and_format_phone(to_number)
    if not formatted_phone:
        return False, "Invalid or empty phone number provided."
    
    review_link = "https://g.page/r/CRWEFyWyuQ5qEAI/review"
    message = (
        f"Hi {first_name}, thank you for choosing Solar Detail!\n\n"
        "We'd love to hear about your experience. Please take a moment to leave us a review:\n"
        f"{review_link}\n\n"
        "Your feedback helps us improve!"
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
        return True, "Review request SMS sent successfully."
    except requests.exceptions.RequestException as e:
        error_details = e.response.json() if e.response else "No response body"
        return False, f"Failed to send review request SMS: {error_details}"

def get_dashboard_stats():
    """
    Calculates total revenue and total clients from the payments.json file.
    """
    payments_file = os.path.join("bot_data", "payments.json")
    
    stats = {
        "dailyRevenue": 0.0,
        "weeklyRevenue": 0.0,
        "monthlyRevenue": 0.0,
        "totalRevenue": 0.0,
        "totalClients": 0
    }
    paid_clients = set()

    if not os.path.exists(payments_file):
        logger.warning("payments.json not found. Returning zero stats.")
        return stats

    try:
        with open(payments_file, "r") as f:
            payments_data = json.load(f)
        
        now = datetime.now()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week_start = today_start - timedelta(days=now.weekday())
        month_start = today_start.replace(day=1)

        for payment in payments_data:
            amount = float(payment.get("amount", 0))
            contact_id = payment.get("contact_id")
            date_str = payment.get("date")

            if amount > 0:
                stats["totalRevenue"] += amount
                if contact_id:
                    paid_clients.add(contact_id)

                if date_str:
                    try:
                        payment_time = datetime.fromisoformat(date_str)
                        if payment_time >= today_start:
                            stats["dailyRevenue"] += amount
                        if payment_time >= week_start:
                            stats["weeklyRevenue"] += amount
                        if payment_time >= month_start:
                            stats["monthlyRevenue"] += amount
                    except ValueError:
                        logger.warning(f"Could not parse date: {date_str}")

    except (json.JSONDecodeError, IOError, ValueError) as e:
        logger.error(f"Error processing payments.json: {e}")
        return stats

    stats["totalClients"] = len(paid_clients)
    return stats

async def get_service_images_and_details(contact_id: str, service_number: int):
    """
    Scans for images and details for a specific service appointment and returns them.
    """
    logger.info(f"Getting images and details for contact {contact_id}, service #{service_number}")
    
    customer_file = os.path.join(CUSTOMER_DATA_DIR, contact_id, "customer_data.json")
    if not os.path.exists(customer_file):
        raise HTTPException(status_code=404, detail="Customer data file not found.")

    service_details = {}
    try:
        with open(customer_file, "r") as f:
            customer_data = json.load(f)
        
        if 0 < service_number <= len(customer_data.get("service_history", [])):
            service_record = customer_data["service_history"][service_number - 1]
            service_details = {
                "service_date": service_record.get("service_date"),
                "service_details": service_record.get("service_details"),
                "quote_amount": service_record.get("quote_amount"),
                "follow_up_date": service_record.get("follow_up_date")
            }
    except (json.JSONDecodeError, IndexError) as e:
        logger.error(f"Error reading service history for {contact_id}: {e}")
        pass

    service_dir = os.path.join(CUSTOMER_DATA_DIR, contact_id, "images", f"service_apt{service_number}")
    if not os.path.isdir(service_dir):
        return {"service_details": service_details, "images": {"before_images": [], "after_images": []}}

    before_urls = []
    after_urls = []

    before_dir = os.path.join(service_dir, "before")
    if os.path.isdir(before_dir):
        for filename in sorted(os.listdir(before_dir)):
            if filename.lower().endswith(('.png', '.jpg', '.jpeg', '.gif')):
                relative_path = os.path.join(contact_id, "images", f"service_apt{service_number}", "before", filename).replace(os.sep, '/')
                url = f"{SERVER_BASE_URL}/images/{relative_path}"
                before_urls.append({"url": url, "filename": filename})

    after_dir = os.path.join(service_dir, "after")
    if os.path.isdir(after_dir):
        for filename in sorted(os.listdir(after_dir)):
            if filename.lower().endswith(('.png', '.jpg', '.jpeg', '.gif')):
                relative_path = os.path.join(contact_id, "images", f"service_apt{service_number}", "after", filename).replace(os.sep, '/')
                url = f"{SERVER_BASE_URL}/images/{relative_path}"
                after_urls.append({"url": url, "filename": filename})
    
    return {
        "service_details": service_details,
        "images": {
            "before_images": before_urls,
            "after_images": after_urls
        }
    }

async def get_random_after_image():
    """
    Scans all customer directories to find all 'after' images and returns a
    randomly selected one.
    """
    import random
    all_after_images = []
    
    if not os.path.exists(CUSTOMER_DATA_DIR):
        raise HTTPException(status_code=500, detail="Customer data directory not found.")

    for contact_id in os.listdir(CUSTOMER_DATA_DIR):
        contact_dir = os.path.join(CUSTOMER_DATA_DIR, contact_id)
        if os.path.isdir(contact_dir):
            images_dir = os.path.join(contact_dir, "images")
            if os.path.isdir(images_dir):
                for service_apt_dir in os.listdir(images_dir):
                    if os.path.isdir(os.path.join(images_dir, service_apt_dir)):
                        after_dir = os.path.join(images_dir, service_apt_dir, "after")
                        if os.path.isdir(after_dir):
                            for filename in os.listdir(after_dir):
                                if filename.lower().endswith(('.png', '.jpg', '.jpeg')):
                                    # Construct the full public URL
                                    relative_path = os.path.join(contact_id, "images", service_apt_dir, "after", filename).replace(os.sep, '/')
                                    url = f"{SERVER_BASE_URL}/images/{relative_path}"
                                    all_after_images.append(url)

    if not all_after_images:
        raise HTTPException(status_code=404, detail="No 'after' images found anywhere.")

    random_image_url = random.choice(all_after_images)
    
    return {"imageUrl": random_image_url}


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
        # To URL like: https://ssh.agencydevworks.ai:8000/images/contactId/service_apt1/before/filename.jpg
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

        # Determine if it's a natural booking or admin-created
        is_natural_booking = service_details.get("solar_cleaning") or service_details.get("pigeon_meshing")

        # --- Message 1: Name, Phone, Address Label ---
        message1_content = ""
        if is_natural_booking:
            message1_content += "**New Client Through Natural Booking**\n"
        
        message1_content += f"**Name:** {full_name}\n"
        message1_content += f"**Phone Number:** {format_phone_for_display(phone_number)}\n"
        message1_content += "**Address:**"
        await new_channel.send(message1_content)

        # --- Message 2: Just the Address ---
        await new_channel.send(full_address)

        # --- Message 3: Service Details, Contacts, Maps ---
        message3_content = ""
        if is_natural_booking:
            services = []
            if service_details.get("solar_cleaning"): services.append("cleaning")
            if service_details.get("pigeon_meshing"): services.append("pigeon mesh")
            
            services_text = "both" if len(services) == 2 else services[0] if len(services) == 1 else "N/A"

            message3_content += f"**# of Panels:** {service_details.get('panel_count', 'N/A')}\n"
            message3_content += f"**Services Requested:** {services_text}\n\n"
        else:
            price_per_panel = service_details.get("price_per_panel", "N/A")
            panel_count = service_details.get('panel_count', 0)
            quote_amount = s_info.get("quote_amount", 0.0)
            
            message3_content += f"**Price Per Panel:** ${price_per_panel} | **# of Panels:** {panel_count}\n"
            message3_content += f"**Quoted:** ${quote_amount:.2f}\n\n"

        message3_content += f"**Add to Contacts:** [Click to Download]({vcard_url})\n"
        message3_content += f"**Apple Maps Link:** {apple_maps_link}"
        await new_channel.send(message3_content)

        # --- Message 4: Warning if no phone number ---
        if not phone_number:
            await new_channel.send(
                "⚠️ **Action Required**: This client was created without a phone number. "
                "A phone number is required to send quotes and gallery links. "
                "Please add one when available to sync with GoHighLevel."
            )

    except Exception as e:
        logger.error(f"An unexpected error occurred in create_customer_channel_and_post: {e}")
        logger.error(traceback.format_exc())

async def create_customer(payload: VercelWebhookPayload):
    """
    Webhook to create a GHL contact, then create a customer folder using the GHL ID.
    If a duplicate contact is found in GHL, it updates the existing record.
    If no phone is provided, a local record is created without GHL integration.
    """
    logger.info(f"Received new customer payload: {payload.formData.model_dump_json(exclude_none=True)}")
    try:
        # Extract the actual form data from the nested object
        form_data = payload.formData
        contact_id = None
        cleaned_phone = ""

        # Determine lastName from lastName or lastInitial
        last_name_to_use = form_data.lastName if form_data.lastName else form_data.lastInitial or ""


        # Conditionally create/update GHL contact if a phone number is provided
        if form_data.phone:
            cleaned_phone = clean_and_format_phone(form_data.phone)
            if not cleaned_phone:
                raise HTTPException(status_code=400, detail="The provided phone number is invalid.")
            
            logger.info("Phone number provided. Attempting to create or find GHL contact...")
            contact_id, is_new = create_ghl_contact(
                first_name=form_data.firstName,
                last_name=last_name_to_use,
                phone=cleaned_phone,
                address=form_data.streetAddress,
                city=form_data.city
            )

            if not contact_id:
                logger.error("create_ghl_contact returned None. Aborting.")
                raise HTTPException(
                    status_code=500, 
                    detail="Failed to create or find contact in GoHighLevel."
                )

            if not is_new:
                logger.info(f"Contact {contact_id} already exists in GHL, attempting to update.")
                update_success = update_ghl_contact(
                    contact_id=contact_id,
                    first_name=form_data.firstName,
                    last_name=last_name_to_use,
                    phone=cleaned_phone,
                    address=form_data.streetAddress,
                    city=form_data.city
                )
                if not update_success:
                    logger.warning(f"Failed to update contact {contact_id} in GHL, but proceeding with local creation anyway.")

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
            "solar_cleaning": form_data.solarCleaning or False,
            "pigeon_meshing": form_data.pigeonMeshing or False,
            "panel_count": form_data.panelCount or 0,
            "price_per_panel": form_data.pricePerPanel,
        }
        
        # Handle potential None for totalAmount
        quote_amount = 0.0
        if form_data.totalAmount:
            try:
                # remove any non-numeric characters like '$'
                cleaned_amount = re.sub(r'[^\d.]', '', form_data.totalAmount)
                if cleaned_amount:
                    quote_amount = float(cleaned_amount)
            except (ValueError, TypeError):
                quote_amount = 0.0

        customer_data = {
            "client_id": contact_id,  # GHL Contact ID is the main ID
            "personal_info": {
                "first_name": form_data.firstName,
                "last_name": last_name_to_use,
                "email": "",
                "phone_number": cleaned_phone,
                "address": full_address,
            },
            "service_history": [
                {
                    "service_date": service_date.isoformat(),
                    "quote_amount": quote_amount,
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

async def add_new_service_to_customer(payload: NewServicePayload):
    """Adds a new service entry to an existing customer's file."""
    contact_id = payload.contactId
    customer_file = os.path.join(CUSTOMER_DATA_DIR, contact_id, "customer_data.json")

    if not os.path.exists(customer_file):
        raise HTTPException(status_code=404, detail=f"Customer file not found for contact ID: {contact_id}")

    try:
        with open(customer_file, "r+") as f:
            customer_data = json.load(f)
            
            new_service = {
                "service_date": datetime.utcnow().isoformat(),
                "quote_amount": float(payload.totalAmount),
                "service_details": {
                    "price_per_panel": float(payload.pricePerPanel),
                    "panel_count": int(payload.panelCount),
                    # Since this is admin-created, we assume no specific services were booked
                    "solar_cleaning": False,
                    "pigeon_meshing": False,
                },
                "follow_up_date": (datetime.utcnow() + timedelta(days=90)).isoformat(),
            }
            
            customer_data.get("service_history", []).append(new_service)
            
            # Rewind and write back
            f.seek(0)
            json.dump(customer_data, f, indent=4)
            f.truncate()

            # Post update to Discord
            channel_id = customer_data.get("discord_channel_id")
            if channel_id:
                channel = client.get_channel(channel_id)
                if channel:
                    # Fetch full name for a more personalized message
                    p_info = customer_data.get("personal_info", {})
                    full_name = f"{p_info.get('first_name', '')} {p_info.get('last_name', '')}".strip()
                    
                    # Safely convert totalAmount to float for formatting
                    total_amount_float = 0.0
                    try:
                        total_amount_float = float(payload.totalAmount)
                    except (ValueError, TypeError):
                        pass # Keep it 0.0 if conversion fails
                    
                    message_content = (
                        f"**New Service Ticket Created for {full_name}**\n\n"
                        f"**Price Per Panel:** ${payload.pricePerPanel} | **# of Panels:** {payload.panelCount}\n"
                        f"**Quoted:** ${total_amount_float:.2f}\n"
                    )
                    await channel.send(message_content)
            return customer_data

    except (IOError, json.JSONDecodeError, ValueError) as e:
        logger.error(f"Error updating customer file for {contact_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to update customer service history.")


# --- Final API Endpoint Definitions ---
# It's good practice to define routes after the functions they call.

@app.get("/api/random-image")
async def final_get_random_after_image():
    return await get_random_after_image()

@app.get("/api/dashboard-stats")
async def final_get_dashboard_stats():
    return get_dashboard_stats()

@app.get("/membership/details")
async def final_get_membership_details(contact_id: str = Query(..., alias="contactId")):
    return get_membership_details(contact_id)

@app.get("/api/service-data/{contact_id}/{service_number}")
async def final_get_service_data(contact_id: str, service_number: int):
    """Unified endpoint to get both images and details for a service appointment."""
    return await get_service_images_and_details(contact_id, service_number)

@app.get("/api/images/{contact_id}")
async def final_get_customer_images(contact_id: str):
    return await get_customer_images(contact_id)

@app.get("/jobs")
def final_get_all_jobs():
    return get_all_jobs()

@app.post("/customer/create")
async def final_create_customer(payload: VercelWebhookPayload):
    return await create_customer(payload)

@app.post("/customer/add-service")
async def final_add_new_service(payload: NewServicePayload):
    return await add_new_service_to_customer(payload)


@app.on_event("startup")
async def startup_event():
    # Start the Discord bot in the background
    asyncio.create_task(client.start(BOT_TOKEN))
    # Start the scheduler
    scheduler.start()
