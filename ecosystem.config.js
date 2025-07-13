module.exports = {
  apps: [{
    name: 'solar-detailers-backend',
    script: 'uvicorn',
    args: [
      'main:app',
      '--host', '0.0.0.0',
      '--port', '8000',
      '--ssl-keyfile=/etc/letsencrypt/live/ssh.agencydevworks.ai/privkey.pem',
      '--ssl-certfile=/etc/letsencrypt/live/ssh.agencydevworks.ai/fullchain.pem'
    ],
    interpreter: 'python3',
    env: {
      // These are variables for all environments
      GHL_LOCATION_ID: 'cWEwz6JBFHPY0LeC3ry3',
      DISCORD_CATEGORY_NAME: 'Solar Detail',
      GHL_SMS_FROM_NUMBER: '+19094049641',
      GHL_API_BASE_URL: 'https://rest.gohighlevel.com/v1'
    },
    env_production: {
      // These variables are only for production
      // You must set these on your server.
      // Do not commit these values to this file.
      // Example: process.env.GHL_API_TOKEN
      GHL_API_TOKEN: process.env.GHL_API_TOKEN,
      GHL_CONVERSATIONS_TOKEN: process.env.GHL_CONVERSATIONS_TOKEN,
      BOT_TOKEN: process.env.BOT_TOKEN,
      DASHBOARD_BASE_URL: 'https://solardetailers.com/dashboard',
      SERVER_BASE_URL: 'https://ssh.agencydevworks.ai:8000',
      FRONTEND_URL: 'https://solardetailers.com'
    }
  }]
}; 