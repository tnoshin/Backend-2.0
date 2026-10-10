import random
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

SPA_TZ = ZoneInfo('America/Los_Angeles')

def seed_slots(db, Slot):
    if Slot.query.count() > 0:
        return
    today = datetime.now(SPA_TZ).date()
    for offset in range(14):
        day = today + timedelta(days=offset)
        weekday = day.weekday()             
        if weekday == 6:
            continue                         
        if weekday == 5:
            open_hour, close_hour = 10, 18  
        else:
            open_hour, close_hour = 9, 20
        for hour in range(open_hour, close_hour):
            db.session.add(Slot(
                start_time=datetime(day.year, day.month, day.day, hour),
                is_booked=random.random() < 0.4
            ))
    db.session.commit()