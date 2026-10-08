# WhatsApp message templates for follow-up reminders

WhatsApp only lets a business write first, to someone who has not messaged it in the last 24 hours, with a **message template that Meta has approved**. A follow-up reminder goes to a patient who may not have written to the clinic for weeks, so these templates are what lets the reminders actually arrive.

Inside the 24-hour window (the patient wrote to the clinic in the last 24 hours) the app sends the same text as a normal message with three buttons. It never needs a template for that.

Until a template is approved **and** ticked in the app, a reminder that cannot go out is not sent. It waits in **Patients > Follow-ups > Send manually** with the exact text, so staff can copy it into WhatsApp themselves.

## What is here

| File | What it is |
|---|---|
| `followup_templates.json` | The six templates, machine-readable: name, language code, category, body with `{{1}}` to `{{5}}`, sample values, footer, buttons. Generated from the same text the app sends (`python -m clinic.followup_notify`), and a test keeps the two identical. |
| this file | What to do in WhatsApp Manager. |

The six templates (two reminders, three languages):

| Name | Sent | Written in | Language code to choose |
|---|---|---|---|
| `followup_reminder_2d_en` | 2 days before the follow-up date | English | `English` (`en`) |
| `followup_reminder_2d_hi` | 2 days before | Hindi, Devanagari script | `Hindi` (`hi`) |
| `followup_reminder_2d_hinglish` | 2 days before | Hindi in Roman script (Hinglish) | `English` (`en`) |
| `followup_reminder_4h_en` | 4 hours before the visit | English | `English` (`en`) |
| `followup_reminder_4h_hi` | 4 hours before | Hindi, Devanagari script | `Hindi` (`hi`) |
| `followup_reminder_4h_hinglish` | 4 hours before | Hindi in Roman script (Hinglish) | `English` (`en`) |

**The Hinglish templates are separate templates in the English language.** Meta has no "Hinglish" language, so they are submitted as English-language templates whose text happens to be Hindi written in English letters. That is allowed; just choose `English` as the language and paste the Hinglish text. The app chooses the template from the language the patient last wrote in (English if it does not know; Hindi in Devanagari for Hindi; Hinglish for Roman-script Hindi).

You do not have to submit all six. A template that is not approved is simply never used, and that language falls back to "Send manually". Start with the English pair if you want to try it first.

## Submitting a template (do this once per template)

1. Open **WhatsApp Manager** for the clinic's WhatsApp Business account (Meta Business Suite > WhatsApp Manager > **Message templates**) and choose **Create template**.
2. **Category: Utility** (`UTILITY` in the JSON). These are reminders about a visit the patient already has, not promotions. (If Meta reclassifies one as Marketing, see "If Meta says no" below.)
3. **Name:** copy it exactly from the table above, for example `followup_reminder_2d_en`. The app looks templates up by this exact name. Names can only use lowercase letters, digits and underscores.
4. **Language:** as in the last column of the table.
5. **Body:** paste the `body.text` of that template from `followup_templates.json` exactly, including the `{{1}}` to `{{5}}` markers. They stand for, in this order: **1** patient name, **2** doctor name, **3** branch, **4** follow-up date, **5** follow-up time. Do not renumber them.
6. **Add sample values.** Meta asks for an example for every variable before it reviews a template. Use the `example` list in the JSON, in order (for English: `Sunita Devi`, `Dr. Mehta`, `Branch A`, `Monday, 12 Oct 2026`, `10:30 AM`). Real patient data does not belong in a sample.
7. **Footer** (optional but recommended): paste `footer.text`. It tells patients how to stop the reminders. The app honours a patient who replies `STOP`.
8. **Buttons:** add **Quick reply** buttons (not Call or URL), exactly three, with these labels in this order:
   * English and Hinglish: `Reschedule`, `Already visited`, `Cancel`
   * Hindi: `समय बदलें`, `विज़िट हो चुकी`, `रद्द करें`

   The label is what the patient sees. What the app gets back when they tap is an id it attaches to each message (`followup:reschedule:<id>`, `followup:visited:<id>`, `followup:cancel:<id>`); you do not enter that anywhere in WhatsApp Manager.
9. **Submit.** Meta reviews it (usually within minutes to a day, sometimes longer). The status changes to **Approved**, **Rejected** or **Paused** in the template list.

## After Meta approves a template

In the app open **Settings**, find **WhatsApp message templates for reminders**, tick the templates that show **Approved** in WhatsApp Manager, and press **Save approved templates**. From then on a reminder to a patient outside the 24-hour window is sent as that template, with the patient's details filled in and the three buttons attached. Reminders that were already waiting in "Send manually" go out when you press **Retry sending** on them.

Untick a template (or leave it unticked) to stop the app using it, for example if Meta pauses it.

## What the app sends, and what it never does

* The text is fixed. Only the five variables change, and they come from the clinic's own records: the patient's name, the doctor, the branch, the date and the time.
* **No diagnosis, treatment or other clinical text is ever included**, in a template or in a normal message. The diagnosis note staff can keep on a follow-up is internal only.
* A tap on a button on the 2-day reminder opens the 24-hour window, so the 4-hour reminder can then go as a normal message.

## Meta's template rules these follow

* The body never starts or ends with a variable, and no two variables are next to each other.
* There is plenty of fixed text around the variables, and the body is well under the 1024 character limit.
* Wording is a polite, short reminder about an existing visit. Nothing promotional, no offers, no calls to "book now". That is what keeps it in the Utility category.
* The footer is under 60 characters, and every button label is under 25.

## Cost

Meta charges for template messages, per message delivered, and the price depends on the message category and the country. **Check Meta's current pricing for India before turning this on**; this folder does not state a rate because it changes. A follow-up can send two template reminders (the 2-day and the 4-hour one) when the patient is outside the 24-hour window; a reminder sent inside the window is an ordinary message, not a template. The clinic's ordinary appointment reminders (the day before and the morning of the visit) are separate messages and are not templates yet.

## If Meta says no

* **Rejected for the variables:** check the body text was pasted exactly (nothing extra at the start or end) and that every variable has a sample value.
* **Reclassified as Marketing:** Meta sometimes does this to anything that looks promotional. You can request a review, or resubmit with the wording unchanged. Marketing templates cost more and can need the patient's opt-in, so ask your Meta contact or check the current rules before accepting the change.
* **Rejected as unclear or wrong language:** make sure the **language** matches the text (Hindi in Devanagari uses `Hindi`; Hindi in Roman letters uses `English`).
* A template that is **Paused** or **Disabled** will not send: untick it in the app's Settings.
