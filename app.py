# from flask import Flask, flash, render_template, request, redirect, url_for
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, flash, session
import sqlite3
import requests
import json
import re
import time 

app = Flask(__name__)
app.secret_key = 'my secert key'  # Important for session security


@app.template_filter('regex_findall')
def regex_findall(s, pattern):
    return re.findall(pattern, s)

TEMPLATES = {
    "order_confirmation": {
        "subject": "Order Confirmation - {{order_id}}",
        "body": "Hello {{customer_name}},\n\nYour order with ID {{order_id}} has been confirmed. Thank you for shopping with us!"
    },
    "shipping_update": {
        "subject": "Shipping Update - {{order_id}}",
        "body": "Hello {{customer_name}},\n\nYour order with ID {{order_id}} has been shipped and is on its way!"
    }
}

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

@app.route('/', methods=['GET', 'POST'])
def index():
    init_db()
    
    # Get all verified numbers from database
    with sqlite3.connect('database.db') as conn:
        cursor = conn.execute('''
            SELECT phone_number_id, verified_name, code_verification_status 
            FROM whatsapp_numbers
            WHERE code_verification_status IN ('APPROVED', 'EXPIRED')
            ORDER BY verified_name
        ''')
        verified_numbers = cursor.fetchall()

    # Handle new number verification
    if request.method == 'POST' and 'waba_id' in request.form:
        waba_id = request.form['waba_id']
        phone_number_id = request.form['phone_number_id']
        access_token = request.form['access_token']

        try:
            # Verify with Facebook API
            url = f'https://graph.facebook.com/v23.0/{waba_id}/phone_numbers'
            params = {'access_token': access_token}
            response = requests.get(url, params=params)
            data = response.json()

            for number in data.get('data', []):
                if number['id'] == phone_number_id:
                    status = number.get('code_verification_status', 'UNKNOWN')
                    
                    # Store in database
                    with sqlite3.connect('database.db') as conn:
                        conn.execute('''
                            INSERT OR REPLACE INTO whatsapp_numbers
                            (phone_number_id, verified_name, code_verification_status, token)
                            VALUES (?, ?, ?, ?)
                        ''', (
                            phone_number_id,
                            number.get('verified_name'),
                            status,
                            access_token
                        ))
                    
                    # Store in session and redirect
                    session['selected_number'] = phone_number_id
                    flash(f'Successfully verified number! Status: {status}', 'success')
                    return redirect(url_for('send_template'))

            flash('Phone number not found in your WABA', 'error')
        
        except Exception as e:
            flash(f'Verification failed: {str(e)}', 'error')

    # Handle selection of existing number
    elif request.method == 'POST' and 'selected_number' in request.form:
        session['selected_number'] = request.form['selected_number']
        return redirect(url_for('send_template'))

    return render_template('index.html', verified_numbers=verified_numbers)




def get_latest_verified_number():
    with sqlite3.connect('database.db') as conn:
        cursor = conn.execute('''
            SELECT phone_number_id, token FROM whatsapp_numbers
            WHERE code_verification_status IN ('APPROVED', 'EXPIRED')
             LIMIT 1
        ''')
        row = cursor.fetchone()
        return (row[0], row[1]) if row else (None, None)

def extract_placeholders(template_body):
    return re.findall(r'{{\s*(\w+)\s*}}', template_body)


@app.route('/send_template', methods=['GET', 'POST'])
def send_template():
    # Verify we have an approved number
     # Get selected number from session or request
    # Verify we have a selected number
    if 'selected_number' not in session:
        flash('No WhatsApp number selected', 'error')
        return redirect(url_for('index'))
    
    phone_number_id = session['selected_number']
    
    # Get access token for selected number
    with sqlite3.connect('database.db') as conn:
        cursor = conn.execute('''
            SELECT token FROM whatsapp_numbers 
            WHERE phone_number_id = ?
        ''', (phone_number_id,))
        result = cursor.fetchone()
        
    if not result:
        flash('Access token not found for selected number', 'error')
        return redirect(url_for('index'))
    
    access_token = result[0]


    # Load user data
    try:
        with open('users.json', 'r') as f:
            users = json.load(f)
    except Exception as e:
        return render_template('error.html', error=f"Error loading users: {str(e)}")

    # Handle template selection (GET request)
    if request.method == 'GET' and 'template_id' in request.args:
        template_id = request.args.get('template_id')
        selected_template = TEMPLATES.get(template_id)
        
        if not selected_template:
            flash('Invalid template selected', 'error')
            return redirect(url_for('send_template'))
        
        placeholders = extract_placeholders(selected_template['body'])
        sample_user_fields = list(users[0].keys()) if users else []
        
        return render_template('map_fields.html',
                           template=selected_template,
                           template_id=template_id,
                           sample_user_fields=sample_user_fields,
                           placeholders=placeholders)

                           

    # Handle field mapping submission (POST request)
    if request.method == 'POST':
        template_id = request.form.get('template_id')
        selected_template = TEMPLATES.get(template_id)
        
        if not selected_template:
            flash('Invalid template', 'error')
            return redirect(url_for('send_template'))
        
        # Validate all mappings
        placeholder_map = {}
        missing_mappings = []
        placeholders = extract_placeholders(selected_template['body'])
        
        for placeholder in placeholders:
            field = request.form.get(f'map_{placeholder}')
            if not field:
                missing_mappings.append(placeholder)
            else:
                placeholder_map[placeholder] = field

        if missing_mappings:
            sample_user_fields = list(users[0].keys()) if users else []
            return render_template('map_fields.html',
                               error=f"Please select fields for: {', '.join(missing_mappings)}",
                               template=selected_template,
                               template_id=template_id,
                               sample_user_fields=sample_user_fields,
                               placeholders=placeholders)

        # Process and send messages
        sent_results = []
        for user in users:
            try:
                message = selected_template['body']
                phone = user.get('phone', '')
                
                # Replace placeholders with user data
                for placeholder, field in placeholder_map.items():
                    value = str(user.get(field, f"[MISSING: {field}]"))
                    message = message.replace(f"{{{{{placeholder}}}}}", value)

                # Prepare WhatsApp API payload
                payload = {
                    "messaging_product": "whatsapp",
                    "to": phone,
                    "type": "text",
                    "text": {"body": message}
                }

                headers = {
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json"
                }

                # In your send_template route, update the message sending part:

                # Send message
                url = f"https://graph.facebook.com/v23.0/{phone_number_id}/messages"
                response = requests.post(url, headers=headers, json=payload)
                response_data = response.json() if response.content else None
                
                # Determine message status
                if response.status_code == 200:
                    status = '✅ Sent'
                    message_id = response_data.get('messages', [{}])[0].get('id', 'N/A')
                    delivery_status = 'Delivered to WhatsApp'  # Initial status
                elif response.status_code == 400:
                    status = f"❌ Failed (Invalid Request)"
                    error_message = response_data.get('error', {}).get('message', 'Unknown error')
                    delivery_status = f"Failed: {error_message}"
                elif response.status_code == 401:
                    status = "❌ Failed (Unauthorized)"
                    delivery_status = "Invalid access token"
                elif response.status_code == 404:
                    status = "❌ Failed (Not Found)"
                    delivery_status = "Phone number ID not found"
                else:
                    status = f"❌ Failed ({response.status_code})"
                    delivery_status = "Unknown error occurred"
                
                # Record result with detailed status
                sent_results.append({
                    'user': user.get('name', 'N/A'),
                    'phone': phone,
                    'message': message,
                    'status': status,
                    'delivery_status': delivery_status,
                    'message_id': message_id if response.status_code == 200 else 'N/A',
                    'timestamp': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    'response': response_data
                })
                
                # For successful sends, you can optionally check delivery status later
                if response.status_code == 200:
                    try:
                        # Check message status after a short delay
                        time.sleep(2)  # Wait 2 seconds before checking status
                        status_url = f"https://graph.facebook.com/v23.0/{message_id}"
                        status_response = requests.get(status_url, headers=headers)
                        status_data = status_response.json() if status_response.content else None
                        
                        if status_response.status_code == 200:
                            current_status = status_data.get('status', 'unknown')
                            if current_status == 'delivered':
                                sent_results[-1]['delivery_status'] = 'Delivered to recipient'
                            elif current_status == 'sent':
                                sent_results[-1]['delivery_status'] = 'Sent (not yet delivered)'
                            elif current_status == 'failed':
                                sent_results[-1]['delivery_status'] = 'Failed to deliver'
                                sent_results[-1]['status'] = '❌ Failed (Delivery)'
                    except Exception as e:
                        sent_results[-1]['delivery_status'] = f"Status check failed: {str(e)}"
                
            except Exception as e:
                sent_results.append({
                    'user': user.get('name', 'N/A'),
                    'phone': user.get('phone', 'N/A'),
                    'message': f"Error: {str(e)}",
                    'status': '❌ Failed'
                })

        return render_template('results.html', 
                           messages=sent_results,
                           template=selected_template)

    # Initial view - show template selection
    return render_template('select_template.html', templates=TEMPLATES)



if __name__ == '__main__':
    init_db()
    app.run(debug=True)