"""Fixed reply templates for the WhatsApp conversation agent
(clinic/conversation.py), in English, Devanagari Hindi and Roman-script
Hinglish.

Hard rule (same as clinic/notify.py): every reply the agent sends is one of
these pre-written strings with dates / times / names inserted by
deterministic code. No model composes or rephrases any of it.

Button and list-row titles live here too, because WhatsApp limits them
(reply button <= 20 chars, list row <= 24 chars); tests assert every title
fits.
"""

LANGUAGES = ("en", "hi", "hinglish")

MSG = {
    # --- menu / greeting -------------------------------------------------
    "menu": {
        "en": "{greet} welcome to the clinic. How can we help you?\nTap an option below, or type 'token' to check your queue token.",
        "hi": "{greet} क्लिनिक में आपका स्वागत है। हम आपकी कैसे मदद कर सकते हैं?\nनीचे दिए विकल्पों में से चुनें, या अपना टोकन जानने के लिए 'टोकन' लिखें।",
        "hinglish": "{greet} clinic mein aapka swagat hai. Hum aapki kaise madad kar sakte hain?\nNeeche diye vikalpon mein se chunein, ya apna token jaanne ke liye 'token' likhein.",
    },
    "menu_unclear": {
        "en": "Sorry, I didn't understand that. What would you like to do?\nYou can also type 'token' to check your queue token.",
        "hi": "क्षमा करें, यह समझ नहीं आया। आप क्या करना चाहेंगे?\nअपना टोकन जानने के लिए 'टोकन' भी लिख सकते हैं।",
        "hinglish": "Maaf kijiye, yeh samajh nahi aaya. Aap kya karna chahenge?\nApna token jaanne ke liye 'token' bhi likh sakte hain.",
    },
    "session_expired": {
        "en": "Our earlier chat timed out. What would you like to do?",
        "hi": "हमारी पिछली बातचीत का समय समाप्त हो गया। आप क्या करना चाहेंगे?",
        "hinglish": "Hamari pichhli baatcheet ka time khatam ho gaya. Aap kya karna chahenge?",
    },
    "thanks": {
        "en": "You're welcome. Send 'hi' any time to see the options.",
        "hi": "आपका स्वागत है। विकल्प देखने के लिए कभी भी 'हाय' लिखें।",
        "hinglish": "Aapka swagat hai. Options dekhne ke liye kabhi bhi 'hi' likhein.",
    },
    # --- branches (only asked when the clinic has more than one) ---------
    "ask_branch": {
        "en": "Which branch would you like to visit?\nTap a branch below, or type your 6-digit PIN code to see the nearest first.",
        "hi": "आप किस ब्रांच में आना चाहेंगे?\nनीचे से ब्रांच चुनें, या सबसे नज़दीकी देखने के लिए अपना 6 अंकों का पिन कोड लिखें।",
        "hinglish": "Aap kis branch mein aana chahenge?\nNeeche se branch chunein, ya sabse najdeeki dekhne ke liye apna 6 digit PIN code likhein.",
    },
    "ask_branch_nearest": {
        "en": "Here are our branches, nearest to PIN code {pin} first.\nTap the one you want.",
        "hi": "पिन कोड {pin} के सबसे नज़दीक वाली ब्रांच पहले दिखाई गई हैं।\nजो चाहिए उसे चुनें।",
        "hinglish": "PIN code {pin} ke sabse najdeek wali branch pehle dikhayi gayi hain.\nJo chahiye use chunein.",
    },
    "ask_branch_retry": {
        "en": "Sorry, I couldn't tell which branch you meant. Tap one below, type its name, or type your 6-digit PIN code.",
        "hi": "क्षमा करें, यह समझ नहीं आया कि आप कौन सी ब्रांच चाहते हैं। नीचे से चुनें, उसका नाम लिखें, या अपना 6 अंकों का पिन कोड लिखें।",
        "hinglish": "Maaf kijiye, samajh nahi aaya aap kaun si branch chahte hain. Neeche se chunein, uska naam likhein, ya apna 6 digit PIN code likhein.",
    },
    "branch_no_slots": {
        "en": "Sorry, {branch} has no free times in the next {days} days. Please choose another branch.",
        "hi": "क्षमा करें, {branch} में अगले {days} दिनों में कोई खाली समय नहीं है। कृपया दूसरी ब्रांच चुनें।",
        "hinglish": "Maaf kijiye, {branch} mein agle {days} dinon mein koi khaali time nahi hai. Kripya doosri branch chunein.",
    },
    # --- a closure moved the appointment (clinic/closure_notify.py); the patient tapped a button ---
    "closure_accepted": {
        "en": "Thank you. Your appointment is confirmed for {date} at {time}.",
        "hi": "धन्यवाद। आपकी अपॉइंटमेंट {date} को {time} के लिए पक्की है।",
        "hinglish": "Dhanyavaad. Aapki appointment {date} ko {time} ke liye pakki hai.",
    },
    "closure_choose_another": {
        "en": "No problem, let's find another time for your appointment on {date} at {time}.",
        "hi": "कोई बात नहीं, {date} को {time} की आपकी अपॉइंटमेंट के लिए दूसरा समय ढूँढते हैं।",
        "hinglish": "Koi baat nahi, {date} ko {time} ki aapki appointment ke liye doosra time dhoondhte hain.",
    },
    "closure_gone": {
        "en": "Sorry, I couldn't find that appointment change. It may already have been updated. What would you like to do?",
        "hi": "क्षमा करें, वह बदलाव नहीं मिला। शायद वह पहले ही अपडेट हो चुका है। आप क्या करना चाहेंगे?",
        "hinglish": "Maaf kijiye, woh badlaav nahi mila. Shayad woh pehle hi update ho chuka hai. Aap kya karna chahenge?",
    },
    "where_branch": {"en": "Branch: {branch}", "hi": "ब्रांच: {branch}", "hinglish": "Branch: {branch}"},
    "where_doctor": {"en": "Doctor: {doctor}", "hi": "डॉक्टर: {doctor}", "hinglish": "Doctor: {doctor}"},
    # --- booking questions -----------------------------------------------
    "ask_name": {
        "en": "Sure. May I have the patient's name for the booking?",
        "hi": "ज़रूर। बुकिंग के लिए कृपया मरीज़ का नाम बताइए।",
        "hinglish": "Zaroor. Booking ke liye kripya mareez ka naam batayein.",
    },
    "ask_name_retry": {
        "en": "Sorry, I couldn't read that as a name. Please type just the name, for example: Sunita Devi.",
        "hi": "क्षमा करें, यह नाम जैसा नहीं लगा। कृपया सिर्फ़ नाम लिखें, जैसे: सुनीता देवी।",
        "hinglish": "Maaf kijiye, yeh naam jaisa nahi laga. Kripya sirf naam likhein, jaise: Sunita Devi.",
    },
    "ask_day": {
        "en": "Which day would you like?\nTap a day below, or type one, for example: tomorrow, Friday or 5 Oct.",
        "hi": "आप किस दिन आना चाहेंगे?\nनीचे से दिन चुनें, या लिखें, जैसे: कल, शुक्रवार या 5/10।",
        "hinglish": "Aap kis din aana chahenge?\nNeeche se din chunein, ya likhein, jaise: kal, Friday ya 5 Oct.",
    },
    "ask_day_changed": {
        "en": "Okay, let's pick again. Which day would you like?\nTap a day below, or type one, for example: tomorrow, Friday or 5 Oct.",
        "hi": "ठीक है, फिर से चुनते हैं। आप किस दिन आना चाहेंगे?\nनीचे से दिन चुनें, या लिखें, जैसे: कल, शुक्रवार या 5/10।",
        "hinglish": "Theek hai, phir se chunte hain. Aap kis din aana chahenge?\nNeeche se din chunein, ya likhein, jaise: kal, Friday ya 5 Oct.",
    },
    "ask_day_resched": {
        "en": "Your appointment is on {date} at {time}.\nWhich day would you like to move it to? Tap a day below, or type one, for example: tomorrow, Friday or 5 Oct.",
        "hi": "आपकी अपॉइंटमेंट {date} को {time} पर है।\nआप इसे किस दिन करना चाहेंगे? नीचे से दिन चुनें, या लिखें, जैसे: कल, शुक्रवार या 5/10।",
        "hinglish": "Aapki appointment {date} ko {time} par hai.\nAap ise kis din karna chahenge? Neeche se din chunein, ya likhein, jaise: kal, Friday ya 5 Oct.",
    },
    "ask_day_retry": {
        "en": "Sorry, I couldn't understand that day. Please type it like: tomorrow, Friday or 5 Oct.",
        "hi": "क्षमा करें, यह दिन समझ नहीं आया। कृपया ऐसे लिखें: कल, शुक्रवार या 5/10।",
        "hinglish": "Maaf kijiye, yeh din samajh nahi aaya. Kripya aise likhein: kal, Friday ya 5 Oct.",
    },
    "offer_slots": {
        "en": "Free times on {date}:\nTap a time below, or type one, for example: 4:30 PM.",
        "hi": "{date} को खाली समय:\nनीचे से समय चुनें, या लिखें, जैसे: शाम 4:30।",
        "hinglish": "{date} ko khaali time:\nNeeche se time chunein, ya likhein, jaise: 4:30 PM.",
    },
    "offer_next_day": {
        "en": "Sorry, there are no free slots on {date}. The next day with openings is {next_date}.\nTap a time below, or type one.",
        "hi": "क्षमा करें, {date} को कोई खाली समय नहीं है। अगला उपलब्ध दिन {next_date} है।\nनीचे से समय चुनें, या लिखें।",
        "hinglish": "Maaf kijiye, {date} ko koi khaali slot nahi hai. Agla available din {next_date} hai.\nNeeche se time chunein, ya likhein.",
    },
    "offer_retry": {
        "en": "Sorry, I couldn't understand that time. Please tap one below, or type a time like 4:30 PM.",
        "hi": "क्षमा करें, यह समय समझ नहीं आया। कृपया नीचे से चुनें, या शाम 4:30 जैसा समय लिखें।",
        "hinglish": "Maaf kijiye, yeh time samajh nahi aaya. Kripya neeche se chunein, ya 4:30 PM jaisa time likhein.",
    },
    "time_taken": {
        "en": "Sorry, {time} on {date} is not available. Please pick one of these:",
        "hi": "क्षमा करें, {date} को {time} उपलब्ध नहीं है। कृपया इनमें से चुनें:",
        "hinglish": "Maaf kijiye, {date} ko {time} available nahi hai. Kripya inmein se chunein:",
    },
    "time_closed": {
        "en": "The clinic is open {hours}. Please pick a time within these hours:",
        "hi": "क्लिनिक का समय {hours} है। कृपया इसी समय के बीच का समय चुनें:",
        "hinglish": "Clinic ka time {hours} hai. Kripya isi time ke beech ka time chunein:",
    },
    "time_past": {
        "en": "That time has already passed today. Please pick a later time:",
        "hi": "आज वह समय बीत चुका है। कृपया बाद का समय चुनें:",
        "hinglish": "Aaj woh time beet chuka hai. Kripya baad ka time chunein:",
    },
    "time_blocked": {
        "en": "Sorry, the clinic isn't taking appointments at {time} on {date}. Please pick one of these:",
        "hi": "क्षमा करें, क्लिनिक {date} को {time} पर अपॉइंटमेंट नहीं ले रहा है। कृपया इनमें से चुनें:",
        "hinglish": "Maaf kijiye, clinic {date} ko {time} par appointments nahi le raha hai. Kripya inmein se chunein:",
    },
    "day_blocked": {
        "en": "Sorry, the clinic isn't taking appointments on {date}. The next day with openings is {next_date}.\nTap a time below, or type one.",
        "hi": "क्षमा करें, क्लिनिक {date} को अपॉइंटमेंट नहीं ले रहा है। अगला उपलब्ध दिन {next_date} है।\nनीचे से समय चुनें, या लिखें।",
        "hinglish": "Maaf kijiye, clinic {date} ko appointments nahi le raha hai. Agla available din {next_date} hai.\nNeeche se time chunein, ya likhein.",
    },
    "slot_just_taken": {
        "en": "Sorry, {time} on {date} was just taken by someone else. Here are other times:",
        "hi": "क्षमा करें, {date} को {time} अभी-अभी किसी और ने ले लिया। ये दूसरे समय उपलब्ध हैं:",
        "hinglish": "Maaf kijiye, {date} ko {time} abhi-abhi kisi aur ne le liya. Yeh doosre time available hain:",
    },
    "slot_blocked": {
        "en": "Sorry, the clinic isn't taking appointments at {time} on {date}. Here are other times:",
        "hi": "क्षमा करें, क्लिनिक {date} को {time} पर अपॉइंटमेंट नहीं ले रहा है। ये दूसरे समय उपलब्ध हैं:",
        "hinglish": "Maaf kijiye, clinic {date} ko {time} par appointments nahi le raha hai. Yeh doosre time available hain:",
    },
    "date_past": {
        "en": "That date has already passed. Which day would you like instead?",
        "hi": "वह तारीख बीत चुकी है। आप किस दिन आना चाहेंगे?",
        "hinglish": "Woh tareekh beet chuki hai. Aap kis din aana chahenge?",
    },
    "date_far": {
        "en": "We can book up to {days} days ahead. Please choose an earlier day.",
        "hi": "हम {days} दिन आगे तक ही बुक कर सकते हैं। कृपया पहले का कोई दिन चुनें।",
        "hinglish": "Hum {days} din aage tak hi book kar sakte hain. Kripya pehle ka koi din chunein.",
    },
    "no_availability": {
        "en": "Sorry, there are no free slots in the next {days} days. A member of our team will reply to you shortly.",
        "hi": "क्षमा करें, अगले {days} दिनों में कोई खाली समय नहीं है। हमारी टीम का कोई सदस्य जल्द ही आपको उत्तर देगा।",
        "hinglish": "Maaf kijiye, agle {days} dinon mein koi khaali slot nahi hai. Hamari team ka koi member jald hi aapko reply karega.",
    },
    "and": {"en": " and ", "hi": " और ", "hinglish": " aur "},
    # --- confirmation summaries -----------------------------------------
    "confirm_book": {
        "en": "Please confirm your request:\nPatient: {name}\nDate: {date}\nTime: {time}",
        "hi": "कृपया अपना अनुरोध कन्फर्म करें:\nमरीज़: {name}\nतारीख: {date}\nसमय: {time}",
        "hinglish": "Kripya apni request confirm karein:\nMareez: {name}\nDate: {date}\nTime: {time}",
    },
    "confirm_reschedule": {
        "en": "Please confirm:\nMove your appointment from {old_date} at {old_time}\nto {date} at {time}.",
        "hi": "कृपया कन्फर्म करें:\nअपनी अपॉइंटमेंट {old_date} को {old_time} से बदलकर\n{date} को {time} करें।",
        "hinglish": "Kripya confirm karein:\nApni appointment {old_date} ko {old_time} se badalkar\n{date} ko {time} par karein.",
    },
    "confirm_cancel": {
        "en": "Cancel your appointment on {date} at {time}?",
        "hi": "{date} को {time} की अपनी अपॉइंटमेंट रद्द करें?",
        "hinglish": "{date} ko {time} ki apni appointment cancel karein?",
    },
    "reask_confirm": {
        "en": "Please tap Confirm request, or Change. You can also reply 'yes' or 'change'.",
        "hi": "कृपया 'कन्फर्म करें' या 'बदलें' दबाएँ। आप 'हाँ' या 'बदलें' भी लिख सकते हैं।",
        "hinglish": "Kripya 'Confirm karein' ya 'Badlein' dabayein. Aap 'haan' ya 'badlein' bhi likh sakte hain.",
    },
    "reask_yesno": {
        "en": "Please tap Yes or No. You can also reply 'yes' or 'no'.",
        "hi": "कृपया 'हाँ' या 'नहीं' दबाएँ। आप 'हाँ' या 'नहीं' लिख भी सकते हैं।",
        "hinglish": "Kripya 'Haan' ya 'Nahi' dabayein. Aap 'haan' ya 'nahi' likh bhi sakte hain.",
    },
    "which_appt": {
        "en": "Which appointment? Tap one from the list.",
        "hi": "कौन सी अपॉइंटमेंट? सूची में से चुनें।",
        "hinglish": "Kaun si appointment? List mein se chunein.",
    },
    "reask_which": {
        "en": "Sorry, I didn't get that. Please tap one of the appointments in the list.",
        "hi": "क्षमा करें, यह समझ नहीं आया। कृपया सूची में से कोई अपॉइंटमेंट चुनें।",
        "hinglish": "Maaf kijiye, yeh samajh nahi aaya. Kripya list mein se koi appointment chunein.",
    },
    # --- outcomes -------------------------------------------------------
    "handoff_received": {
        "en": "Request received -- the clinic will confirm shortly.",
        "hi": "अनुरोध मिल गया है -- क्लिनिक जल्द ही कन्फर्म करेगा।",
        "hinglish": "Request mil gayi hai -- clinic jald hi confirm karega.",
    },
    "already_submitted": {
        "en": "Your request has already been received -- the clinic will confirm shortly.",
        "hi": "आपका अनुरोध पहले ही मिल चुका है -- क्लिनिक जल्द ही कन्फर्म करेगा।",
        "hinglish": "Aapki request pehle hi mil chuki hai -- clinic jald hi confirm karega.",
    },
    "already_done": {
        "en": "That is already done -- you will find the details in the confirmation message above. Send 'hi' for the options.",
        "hi": "यह पहले ही हो चुका है -- विवरण ऊपर के कन्फर्मेशन संदेश में हैं। विकल्पों के लिए 'हाय' लिखें।",
        "hinglish": "Yeh pehle hi ho chuka hai -- details upar ke confirmation message mein hain. Options ke liye 'hi' likhein.",
    },
    # Safety net only: normally the booking / cancel / reschedule confirmation
    # (clinic/notify.py templates) is what the patient receives. These are sent
    # instead if that confirmation could not be queued for some reason.
    "auto_done_book": {
        "en": "Your appointment is booked for {date} at {time}.",
        "hi": "आपकी अपॉइंटमेंट {date} को {time} पर बुक हो गई है।",
        "hinglish": "Aapki appointment {date} ko {time} par book ho gayi hai.",
    },
    "auto_done_cancel": {
        "en": "Your appointment on {date} at {time} has been cancelled.",
        "hi": "{date} को {time} की आपकी अपॉइंटमेंट रद्द कर दी गई है।",
        "hinglish": "{date} ko {time} ki aapki appointment cancel kar di gayi hai.",
    },
    "auto_done_reschedule": {
        "en": "Your appointment has been moved to {date} at {time}.",
        "hi": "आपकी अपॉइंटमेंट अब {date} को {time} पर है।",
        "hinglish": "Aapki appointment ab {date} ko {time} par hai.",
    },
    "nothing_cancelled": {
        "en": "Okay, nothing has been cancelled.",
        "hi": "ठीक है, कुछ भी रद्द नहीं किया गया।",
        "hinglish": "Theek hai, kuch bhi cancel nahi kiya gaya.",
    },
    "no_appt": {
        "en": "I couldn't find an upcoming appointment under this number. Would you like to book one?",
        "hi": "इस नंबर पर कोई आने वाली अपॉइंटमेंट नहीं मिली। क्या आप नई बुक करना चाहेंगे?",
        "hinglish": "Is number par koi upcoming appointment nahi mili. Kya aap nayi book karna chahenge?",
    },
    "dup_request": {
        "en": "We already have a request for that appointment -- the clinic will confirm shortly.",
        "hi": "उस अपॉइंटमेंट के लिए हमें पहले से अनुरोध मिल चुका है -- क्लिनिक जल्द ही कन्फर्म करेगा।",
        "hinglish": "Us appointment ke liye hamare paas pehle se request hai -- clinic jald hi confirm karega.",
    },
    "cap_reached": {
        "en": "You already have {n} upcoming appointments or pending requests, which is the maximum. Please cancel one first, or call the clinic.",
        "hi": "आपकी पहले से {n} आने वाली अपॉइंटमेंट या लंबित अनुरोध हैं, जो अधिकतम सीमा है। कृपया पहले एक रद्द करें, या क्लिनिक को कॉल करें।",
        "hinglish": "Aapki pehle se {n} upcoming appointments ya pending requests hain, jo maximum hai. Kripya pehle ek cancel karein, ya clinic ko call karein.",
    },
    "status_none": {
        "en": "You have no upcoming appointment under this number. Would you like to book one?",
        "hi": "इस नंबर पर आपकी कोई आने वाली अपॉइंटमेंट नहीं है। क्या आप नई बुक करना चाहेंगे?",
        "hinglish": "Is number par aapki koi upcoming appointment nahi hai. Kya aap nayi book karna chahenge?",
    },
    # --- safety ---------------------------------------------------------
    "emergency": {
        "en": "If this is an emergency please call 112 (ambulance 108) or go to the nearest hospital now. The clinic has been alerted.",
        "hi": "यदि यह आपातकाल है तो कृपया अभी 112 (एम्बुलेंस 108) पर कॉल करें या नज़दीकी अस्पताल जाएँ। क्लिनिक को सूचित कर दिया गया है।",
        "hinglish": "Agar yeh emergency hai to kripya abhi 112 (ambulance 108) par call karein ya najdeeki hospital jayein. Clinic ko alert kar diya gaya hai.",
    },
    "clinical": {
        "en": "We can't give medical advice on chat. Please call the clinic or book a consultation.",
        "hi": "हम चैट पर चिकित्सीय सलाह नहीं दे सकते। कृपया क्लिनिक को कॉल करें या परामर्श बुक करें।",
        "hinglish": "Hum chat par medical advice nahi de sakte. Kripya clinic ko call karein ya consultation book karein.",
    },
    "escalate": {
        "en": "A member of our team will reply to you shortly.",
        "hi": "हमारी टीम का कोई सदस्य जल्द ही आपको उत्तर देगा।",
        "hinglish": "Hamari team ka koi member jald hi aapko reply karega.",
    },
}

# Reply-button titles (<= 20 chars) and list button labels (<= 20 chars).
BTN = {
    "book": {"en": "Book appointment", "hi": "बुक करें", "hinglish": "Book karein"},
    "reschedule": {"en": "Reschedule", "hi": "समय बदलें", "hinglish": "Time badlein"},
    "cancel": {"en": "Cancel appointment", "hi": "रद्द करें", "hinglish": "Cancel karein"},
    "confirm": {"en": "Confirm request", "hi": "कन्फर्म करें", "hinglish": "Confirm karein"},
    "change": {"en": "Change", "hi": "बदलें", "hinglish": "Badlein"},
    "yes": {"en": "Yes", "hi": "हाँ", "hinglish": "Haan"},
    "no": {"en": "No", "hi": "नहीं", "hinglish": "Nahi"},
    "choose_time": {"en": "Choose time", "hi": "समय चुनें", "hinglish": "Time chunein"},
    "choose": {"en": "Choose", "hi": "चुनें", "hinglish": "Chunein"},
    "choose_branch": {"en": "Choose branch", "hi": "ब्रांच चुनें", "hinglish": "Branch chunein"},
}

# Short notes shown under a branch in the "Which branch?" list (<= 72 chars with the address).
ROW_NOTE = {
    "last_visit": {"en": "Your last visit", "hi": "आपकी पिछली विज़िट", "hinglish": "Aapki pichhli visit"},
    "your PIN code": {"en": "Your PIN code area", "hi": "आपके पिन कोड का इलाका", "hinglish": "Aapke PIN code ka ilaaka"},
    "very close": {"en": "Very close", "hi": "बहुत नज़दीक", "hinglish": "Bahut najdeek"},
    "close by": {"en": "Close by", "hi": "पास में", "hinglish": "Paas mein"},
    "nearby": {"en": "Nearby", "hi": "आस-पास", "hinglish": "Aas-paas"},
}

DAY_WORDS = {
    "today": {"en": "Today", "hi": "आज", "hinglish": "Aaj"},
    "tomorrow": {"en": "Tomorrow", "hi": "कल", "hinglish": "Kal"},
}

_GREET_WORD = {"en": "Hello", "hi": "नमस्ते", "hinglish": "Namaste"}


def norm_lang(language):
    """Anything that isn't a language we have text for (e.g. 'bilingual')
    falls back to English."""
    return language if language in LANGUAGES else "en"


def greet(language, name=None):
    word = _GREET_WORD[norm_lang(language)]
    return "{} {},".format(word, name) if name else "{},".format(word)


def text(key, language, **values):
    return MSG[key][norm_lang(language)].format(**values)


def row_note(key, language):
    return ROW_NOTE[key][norm_lang(language)]


def button(key, language):
    return BTN[key][norm_lang(language)]
