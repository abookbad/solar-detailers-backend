# Solar Detailing Customer API

This is a FastAPI application to manage customer data for a solar detailing business. It provides a webhook to create new customers and stores their information in a structured way.

## Features

- **Create Customer:** A webhook endpoint to create a new customer.
- **Data Storage:** Each customer's data is stored in a separate JSON file within a unique directory.
- **Unique ID:** A unique client ID is generated for each customer.
- **Follow-up Tracking:** Automatically calculates a follow-up date 3 months after the service date.

## Project Structure

```
.
├── customer_data/
├── .gitignore
├── main.py
├── README.md
└── requirements.txt
```

## Setup and Installation

### Prerequisites

- Python 3.7+
- pip

### Installation

1.  **Clone the repository:**
    ```bash
    git clone <repository-url>
    cd solar-detail-discord-bot
    ```

2.  **Create a virtual environment (recommended):**
    ```bash
    python -m venv venv
    source venv/bin/activate  # On Windows, use `venv\Scripts\activate`
    ```

3.  **Install the dependencies:**
    ```bash
    pip install -r requirements.txt
    ```

## Running the Application

To run the combined web server and Discord bot, use the following command:

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

The application will be available at `http://<your-ip-address>:8000`, and the Discord bot will connect automatically. For local testing, you can use `http://127.0.0.1:8000`.

## API Documentation

Once the application is running, you can access the interactive API documentation at `http://127.0.0.1:8000/docs`.

## Webhook Usage

To create a new customer, send a `POST` request to the `/customer/create` endpoint with the following JSON payload:

```json
{
  "first_name": "John",
  "last_name": "Doe",
  "email": "john.doe@example.com",
  "phone_number": "123-456-7890",
  "address": "123 Main St, Anytown, USA",
  "service_date": "2024-01-15T10:00:00Z",
  "quote_amount": 500.00,
  "service_details": "Full solar panel cleaning and inspection."
}
```

### Successful Response

```json
{
  "message": "Customer created successfully",
  "client_id": "a_unique_client_id"
}
```

## Deployment

This application is ready to be deployed on a server (e.g., an Ubuntu server with Nginx and Gunicorn). You can use `git` to transfer the files to your server. 