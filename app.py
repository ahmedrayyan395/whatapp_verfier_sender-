from flask import Flask, render_template, request, redirect, url_for
import sqlite3
import requests
import json
import re
import time 

app = Flask(__name__)

@app.template_filter('regex_findall')
def regex_findall(s, pattern):
    return re.findall(pattern, s)


TEMPLATES  = {
    "order_confirmation": {
        "subject": "Order Confirmation - {{order_id}}",
        "body": "Hello {{customer_name}},\n\nYour order with ID {{order_id}} has been confirmed. Thank you for shopping with us!"
    },
    "shipping_update": {
        "subject": "Shipping Update - {{order_id}}",
        "body": "Hello {{customer_name}},\n\nYour order with ID {{order_id}} has been shipped and is on its way!"
    }
}




# 1. Init DB
def init_db():
    with sqlite3.connect('database.db') as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS whatsapp_numbers (
                phone_number_id TEXT PRIMARY KEY,
                verified_name TEXT,
                code_verification_status TEXT,
                token TEXT
            )
        ''')

# 2. Index page with input form
@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        waba_id = request.form['waba_id']
        phone_number_id = request.form['phone_number_id']
        access_token = request.form['access_token']

        url = f'https://graph.facebook.com/v23.0/{waba_id}/phone_numbers'
        params = {'access_token': access_token}
        response = requests.get(url, params=params)
        data = response.json()

        print(data)

        for number in data.get('data', []):
            if number['id'] == phone_number_id:
                status = number.get('code_verification_status', 'UNKNOWN')
                if status in ['APPROVED', 'EXPIRED']:
                    with sqlite3.connect('database.db') as conn:
                        conn.execute('''
                            INSERT OR REPLACE INTO whatsapp_numbers
                            (phone_number_id, verified_name, code_verification_status, token)
                            VALUES (?, ?, ?, ?)
                        ''', (phone_number_id, number['verified_name'], status, access_token))
                    return redirect(url_for('send_template', name=number['verified_name']))
                else:
                    return f"Phone number found but status '{status}' is not accepted."

        return "Phone number not found in WABA data."
    return render_template('index.html')



def get_latest_verified_number():
    with sqlite3.connect('database.db') as conn:
        cursor = conn.execute('''
            SELECT phone_number_id, token FROM whatsapp_numbers
            WHERE code_verification_status IN ('APPROVED', 'EXPIRED')
             LIMIT 1
        ''')
        row = cursor.fetchone()
        return (row[0], row[1]) if row else (None, None)





# 3. Choose a template to send
import re

def extract_placeholders(template_body):
    return re.findall(r'{{\s*(\w+)\s*}}', template_body)

@app.route('/send_template', methods=['GET', 'POST'])
def send_template():
    phone_number_id, access_token = get_latest_verified_number()
    if not phone_number_id:
        return "No verified WhatsApp number available."

    try:
        with open('users.json', 'r') as f:
            users = json.load(f)
    except Exception as e:
        return f"Error loading users.json: {str(e)}"

    sample_user = users[0] if users else {}

    template_id = request.form.get('template_id') if request.method == 'POST' else None
    selected_template = TEMPLATES.get(template_id) if template_id else None
    placeholders = extract_placeholders(selected_template['body']) if selected_template else []

    if request.method == 'POST' and selected_template:
        placeholder_map = {}
        for placeholder in placeholders:
            user_field = request.form.get(f'map_{placeholder}')
            if not user_field:
                return f"Missing mapping for placeholder: {placeholder}"
            placeholder_map[placeholder] = user_field

        sent_results = []

        for user in users:
            message = selected_template['body']
            for placeholder, user_field in placeholder_map.items():
                value = user.get(user_field, f"[{user_field}]")
                message = message.replace(f"{{{{{placeholder}}}}}", value)

            payload = {
                "messaging_product": "whatsapp",
                "to": user['phone'],
                "type": "text",
                "text": {
                    "body": message
                }
            }

            headers = {
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json"
            }

            url = f"https://graph.facebook.com/v23.0/{phone_number_id}/messages"
            response = requests.post(url, headers=headers, json=payload)

            status = '✅ Sent' if response.status_code == 200 else f"❌ Failed ({response.status_code})"
            sent_results.append({
                'user': user.get('name', 'N/A'),
                'phone': user['phone'],
                'message': message,
                'status': status
            })

        return render_template('result.html', messages=sent_results, template=selected_template)

    return render_template(
        'send_template.html',
        templates=TEMPLATES,
        sample_user=sample_user,
        template_id=template_id,
        placeholders=placeholders
    )

# 4. Run the app
if __name__ == '__main__':
    init_db()
    app.run(debug=True)
