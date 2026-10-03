import requests

def send_telegram_message(message):
    bot_token = os.getenv('SHARED_TELEGRAM_BOT_TOKEN')
    chat_id = os.getenv('SHARED_TELEGRAM_CHAT_ID')
    url = f'https://api.telegram.org/bot{bot_token}/sendMessage'
    
    params = {
        'chat_id': chat_id,
        'text': message
    }
    
    requests.post(url, params=params)
  
