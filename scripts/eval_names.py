"""Check the name extractor against labelled commands (live Ollama, read-only).

    PYTHONPATH=. .venv/bin/python scripts/eval_names.py
"""

import statistics
import time

from clinic.nlu.llm_slots import extract_name

CASES = [
    ("एक नया पेशेंट है अमित दुआ, उसका 7 अक्टूबर को सुबह 12 बजे अपॉइंटमेंट फिक्स करो।", "अमित दुआ"),
    ("एक नया पेशेंट है अमित नारंग, उसका 6 अक्टूबर को सुबह 11 बजे अपॉइंटमेंट फिक्स करो।", "अमित नारंग"),
    ("Can you book an appointment for a patient named Amit Dua on fourth of October at 2 PM?", "Amit Dua"),
    ("book an appointment for Ramesh tomorrow at 5 pm", "Ramesh"),
    ("Sunita Devi ke liye kal subah 10 baje appointment book karo", "Sunita Devi"),
    ("cancel Ramesh's appointment", "Ramesh"),
    ("Sunita ka appointment cancel kar do", "Sunita"),
    ("रीना की अपॉइंटमेंट कैंसिल करो", "रीना"),
    ("what is Rakesh Verma's phone number", "Rakesh Verma"),
    ("Retrieve the patient data for Rakesh Verma", "Rakesh Verma"),
    ("Sunita Devi ka number batao", "Sunita Devi"),
    ("register a new patient Ravi Kumar phone 9876543210 age 42", "Ravi Kumar"),
    ("naya patient Geeta Sharma 9123456780 umar 35", "Geeta Sharma"),
    ("Ramesh came in today fee 500 rupees", "Ramesh"),
    ("Mohan ki visit log karo fees 400", "Mohan"),
    ("set a follow-up for Priya in 7 days", "Priya"),
    ("Anil ko 10 din baad follow up par bulao", "Anil"),
    ("mark Seema present today", "Seema"),
    ("Rahul aaj absent hai", "Rahul"),
    ("मोहन शर्मा का फॉलो अप कैंसिल करो", "मोहन शर्मा"),
    ("get all appointments for tomorrow", None),
    ("what is today's cash book", None),
    ("how many patients are waiting", None),
]

if __name__ == "__main__":
    extract_name("warm up Ramesh")
    wrong, times = [], []
    for text, expected in CASES:
        start = time.perf_counter()
        got = extract_name(text)
        times.append(time.perf_counter() - start)
        if (got or None) != expected:
            wrong.append((text, expected, got))
    print("%d/%d correct | median %.2fs, max %.2fs" % (len(CASES) - len(wrong), len(CASES), statistics.median(times), max(times)))
    for text, expected, got in wrong:
        print("  MISS %r\n       expected=%r got=%r" % (text, expected, got))
