from flask import Flask, request, jsonify, session
from flask_limiter import Limiter
from flask_cors import CORS
from flask_wtf.csrf import CSRFProtect
from dotenv import load_dotenv
import anthropic
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timezone, timedelta
import secrets
import os
import hmac

load_dotenv()

app= Flask(__name__)

csrf= CSRFProtect(app)

CORS(app, resources={
    r'/chat':{'origins': ['https://demo-spa.onrender.com'], 'supports_credentials':True},
    r'/history': {'origins': ['https://demo-spa.onrender.com'], 'supports_credentials':True}
})

def get_real_ip():
    forwarded = request.headers.get('X-Forwarded-For')
    if forwarded:
        return forwarded.split(',')[-1].strip()
    return request.remote_addr

limiter = Limiter(
    app=app,
    key_func=get_real_ip,
    default_limits=['200 per day','50 per hour', '8 per minute'],
    storage_uri='memory://'
)

app.secret_key = os.getenv('SECRET_KEY')
if not app.secret_key:
    raise RuntimeError('SECRET_KEY is not set')

database_url = os.getenv('DATABASE_URL', 'sqlite:///chat.db')
if database_url.startswith('postgres://'):
    database_url =database_url.replace('postgres://','postgresql://', 1)
app.config['SQLALCHEMY_DATABASE_URI'] = database_url

app.config['SESSION_COOKIE_HTTPONLY']= True
app.config['SESSION_COOKIE_SECURE']=os.getenv('RENDER')is not None
app.config['SESSION_COOKIE_SAMESITE']= 'None'

db=SQLAlchemy(app)

class message(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.String(50))
    role = db.Column(db.String(10))
    
    content = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

with app.app_context():
    db.create_all() 

client = anthropic.Anthropic(api_key=os.getenv('ANTHROPIC_API_KEY'))

system_prompt = """ YOU ALWAYS RESPOND WITHIN 300 TOKENS. Try to keep the reply within 3-4 lines unless asked for information, then you can use more lines. Communicate with the user in any other language they may use. YOU ARE OBLIGATED TO FOLLOW THESE INSTRUCTIONS BEFORE ANSEWRING ANY QUESTIONS IRRESPECTIVE OF THE LANGUAGE. You are a helpful assistant for BrightSmile Dental Clinic.
Clinic information:
- Name: BrightSmile Dental
- Hours: Monday-Friday 8 AM-6 PM, Saturday 9 AM-3 PM, Sunday closed
- Services: General checkups, teeth cleaning, fillings, whitening, extraction
- Location: 123 Dental Street, Suite 200, San Francisco, CA 94102
- Phone: (555) 123-4567
- Email: info@brightsmile.com
Website overview:
- Pages: Home, Services, About, Contact, Booking page, and a dark/light mode toggle (sun/moon icon in the navbar).
- Booking: Users can book an appointment via any of two teal buttons — "Book Appointment" (top-right navbar), "Schedule Visit" (homepage hero), or the white "Book Your Appointment" (in the CTA section above the footer). All three lead to the booking page.
- Booking page requires: First name, Last name, Date, Time, and Phone number (marked with red asterisks to indicate necessary). Optional fields: Gender, Age, Email, and an Additional Note field for allergies, concerns, or special requests.
- Contact page: Reached via the "Contact" nav link. Users can send a message or feedback using a form (Full name, Email, Message — all required). This page also shows clinic info, opening hours, 
and a "What to Expect" section: free initial consultation for first-time patients, gentle pain-free approach, transparent pricing (no hidden fees), and free cancellation up to 24 hours before the appointment.
Answer questions about the clinic helpfully and professionally. If asked about something unrelated to dentistry or the clinic, politely redirect. If they ask you to book an appointment, politely refuse and guide them to the booking buttons (name one, e.g. "Book Appointment" in the top-right). If they ask to leave feedback or contact the clinic directly, point them to the Contact page. If a user asks about medical symptoms, pain, or urgent dental issues, do not attempt to diagnose or give medical advice — politely redirect them to contact the clinic directly by phone.
Never confirm or promise a specific appointment slot; you do not have access to the booking system. Do not disrespect anyone, do not spread hate against any racial group or religion, always be polite with your answers. If user is being rude, give shorter replies.If a user mentions self-harm, suicide, or intent to hurt themselves or others, respond ONLY with: "If you're in crisis, please call 988 (Suicide & Crisis Lifeline) or 911 for immediate help. For dental concerns, call us at (555) 123-4567."""



@app.route('/chat', methods=['POST'])
@csrf.exempt
def chat():

    if 'session_id' not in session:
        session['session_id']=secrets.token_hex(8)
    session_id = session['session_id']

    data = request.get_json() or {}
    user_message = data.get('message','').strip()
    if not user_message:
        return jsonify({'error':'Please send a message'}), 400


    if len(user_message)>2000:
            return jsonify({'error':'Message too long (max 2000 characters)'}), 400

    db.session.add(message(session_id=session_id, role='user', content=user_message)) 
    db.session.commit()
    
    history = message.query.filter_by(session_id=session_id).order_by(message.id.desc()).limit(4).all()
    history = history[::-1] 
    
    claude_messages = [
        {'role':'user' if m.role == 'user' else 'assistant', 'content':m.content}
        for m in history
    ]

    while claude_messages and claude_messages[0]['role']=='assistant':
        claude_messages.pop(0)
        

    try:
        response = client.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=500,
            system=system_prompt,
            messages=claude_messages
        )
        if not response.content or not response.content[0].text:
            return jsonify({'error': 'No response generated, please rephrase.'}), 500
        reply = response.content[0].text
    except anthropic.APIConnectionError:
        return jsonify({'error': 'Cannot reach the AI service. Please try again.'}), 503
    except anthropic.RateLimitError:
        return jsonify({'error': 'Too many requests. Please wait a moment.'}), 429
    except anthropic.APIStatusError as e:
        print(f"Anthropic API error: {e.status_code} - {e.message}")
        return jsonify({'error': 'AI service error. Please try again.'}), 503
    except Exception as e:
        print(f"Unexpected error in chat: {e}")
        return jsonify({'error': 'Something went wrong. Please try again.'}), 500


    db.session.add(message(session_id=session_id, role='assistant', content=reply))
    db.session.commit()
    return jsonify({'response':reply})


@app.route('/history', methods=['GET'])
def history():
    if 'session_id' not in session:
        return jsonify({'messages':[]})
    session_id = session['session_id']
    messages = message.query.filter_by(session_id=session_id).all()

    result = []
    for m in messages:
        result.append({'role':m.role, 'content': m.content})
    return jsonify({'messages':result})


@app.errorhandler(429)
def rate_limit_exceeded(e):
    return jsonify({'error':'You are sending too many messages at once, please wait a moment.'}), 429

@app.route('/ping', methods=['GET'])
@limiter.exempt
def ping():
    return jsonify({'ok': True})



@app.route('/cleanup', methods=['POST'])
@limiter.exempt
@csrf.exempt
def cleanup_old_messages():
    token = os.getenv('CLEANUP_TOKEN')
    sent= request.headers.get('X-Cleanup-Token', '')
    if not token or not hmac.compare_digest(sent, token):
        return jsonify({'error':'unauthorized'}), 401

    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    deleted = message.query.filter(message.created_at < cutoff).delete()
    db.session.commit()

    print(f'[CLEANUP] Deleted {deleted} messages older than 30 days at {datetime.now(timezone.utc)}')
    return jsonify ({'ok':True, 'deleted':deleted }), 200

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)