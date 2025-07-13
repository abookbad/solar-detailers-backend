import json
from datetime import datetime, timedelta
import pytz
import os

# --- Configuration ---
CALENDAR_FILE = "calendar.json"
TIMEZONE = pytz.timezone("America/Los_Angeles")
BUSINESS_START_HOUR = 7  # 7 AM
BUSINESS_END_HOUR = 21   # Last slot starts at 8 PM, ends at 9 PM
APPOINTMENT_DURATION_HOURS = 1

# --- Helper Functions ---
def _load_appointments() -> list:
    """Loads all appointments from the JSON file."""
    if not os.path.exists(CALENDAR_FILE):
        return []
    try:
        with open(CALENDAR_FILE, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, FileNotFoundError):
        return []

def _save_appointments(appointments: list):
    """Saves a list of appointments to the JSON file."""
    with open(CALENDAR_FILE, "w") as f:
        json.dump(appointments, f, indent=4)

def get_appointments_for_day(target_date: datetime.date) -> list:
    """
    Returns a list of all appointments scheduled for a specific date, normalized to the business timezone.
    """
    appointments = _load_appointments()
    daily_appointments = []

    for appt in appointments:
        appt_time = datetime.fromisoformat(appt["start_time"])
        # Normalize to the business's timezone before comparing the date
        if appt_time.astimezone(TIMEZONE).date() == target_date:
            daily_appointments.append(appt)

    # Sort appointments by start time
    daily_appointments.sort(key=lambda x: x["start_time"])
    return daily_appointments

def get_available_slots(target_date: datetime.date) -> list[str]:
    """
    Generates a list of available 1-hour appointment slots for a given date
    within business hours (7 AM - 7 PM PST).
    """
    booked_start_times = {
        datetime.fromisoformat(appt["start_time"]).astimezone(TIMEZONE)
        for appt in get_appointments_for_day(target_date)
    }

    available_slots = []
    
    # Start from the beginning of the business day in the specified timezone
    day_start = TIMEZONE.localize(datetime(
        target_date.year, target_date.month, target_date.day, BUSINESS_START_HOUR
    ))

    # Iterate through all possible slots in the day
    for i in range(BUSINESS_START_HOUR, BUSINESS_END_HOUR):
        slot_time = TIMEZONE.localize(datetime(
            target_date.year, target_date.month, target_date.day, i
        ))

        # Check if the slot is in the future and not already booked
        if slot_time > datetime.now(TIMEZONE) and slot_time not in booked_start_times:
            available_slots.append(slot_time.isoformat())
            
    return available_slots

def get_bulk_available_slots(days_in_advance: int) -> dict:
    """
    Generates a dictionary of available slots for a specified number of days in advance.
    The keys are dates (YYYY-MM-DD) and values are lists of available ISO time strings.
    """
    all_available_slots = {}
    today = datetime.now(TIMEZONE).date()

    for i in range(days_in_advance):
        target_date = today + timedelta(days=i)
        date_str = target_date.isoformat()
        all_available_slots[date_str] = get_available_slots(target_date)

    return all_available_slots

def book_appointment(contact_id: str, start_time_iso: str) -> (bool, str):
    """
    Books an appointment for a contact if the slot is available.
    This function now contains more robust, atomic-like checking.
    """
    try:
        start_time = datetime.fromisoformat(start_time_iso).astimezone(TIMEZONE)
    except ValueError:
        return False, "Invalid ISO date format provided."

    # --- Direct Booking Validation ---

    # 1. Check if it's a valid time within business hours (on the hour)
    if start_time.minute != 0 or start_time.second != 0 or start_time.microsecond != 0:
        return False, "Appointments can only be booked on the hour."
    if not (BUSINESS_START_HOUR <= start_time.hour < BUSINESS_END_HOUR):
        return False, "The requested time is outside of business hours."
    if start_time < datetime.now(TIMEZONE):
        return False, "Cannot book appointments in the past."

    # 2. Load the most current appointment data and check for a direct collision
    appointments = _load_appointments()
    is_booked = any(
        datetime.fromisoformat(appt["start_time"]).astimezone(TIMEZONE) == start_time
        for appt in appointments
    )

    if is_booked:
        return False, "The requested time slot is not available."

    # 3. If all checks pass, create and save the appointment
    new_appointment = {
        "contact_id": contact_id,
        "start_time": start_time.isoformat(),
        "end_time": (start_time + timedelta(hours=APPOINTMENT_DURATION_HOURS)).isoformat(),
        "booked_at": datetime.now(TIMEZONE).isoformat()
    }

    appointments.append(new_appointment)
    _save_appointments(appointments)

    return True, f"Appointment successfully booked for {contact_id} at {start_time.strftime('%Y-%m-%d %I:%M %p %Z')}." 