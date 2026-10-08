// The "a phone number is required to book" rule, for the screens (the server decides: clinic/booking_phone.py has the
// same rule and the same wording). Pure and DOM-free so tests/booking_phone.test.js can run it under node.
//
//   BookingPhone.valid(phone)               the 10 digits of a phone, or "" (last 10 digits, so +91 / 0 / spaces do not matter)
//   BookingPhone.problem(slots, patients)   null, or the message to show for a booking card / form
//   BookingPhone.required(slots, patients)  true when the booking depends on the phone typed on it
//
// `patients` is the page's patient list ({id, name, phone}). A registered patient's own number counts; so does
// the one typed on the booking. A patient the list does not know is left to the server (null).

window.BookingPhone = (function () {
  var REQUIRED = "A phone number is required to book an appointment. Enter the patient's 10-digit phone number.";
  var PATIENT_NO_PHONE = "This patient has no valid phone number on file. A phone number is required to book an appointment: add or correct the patient's 10-digit phone number first.";

  function valid(phone) {
    if (phone === null || phone === undefined) return "";
    var digits = String(phone).replace(/\D/g, "");
    if (digits.length >= 10) digits = digits.slice(-10);
    return digits.length === 10 ? digits : "";
  }

  function problem(slots, patients) {
    slots = slots || {};
    var id = slots.patient_id;
    if (id !== null && id !== undefined && id !== "") {
      var found = null;
      (patients || []).forEach(function (p) { if (String(p.id) === String(id)) found = p; });
      if (!found) return null;
      if (valid(found.phone) || valid(slots.patient_phone)) return null;
      return PATIENT_NO_PHONE;
    }
    return valid(slots.patient_phone) ? null : REQUIRED;
  }

  // True when the booking depends on the phone typed on it: no registered patient picked, or the picked one has
  // no valid number on file. (A patient the list does not know: false, the server decides.)
  function required(slots, patients) {
    slots = slots || {};
    var id = slots.patient_id;
    if (id === null || id === undefined || id === "") return true;
    var found = null;
    (patients || []).forEach(function (p) { if (String(p.id) === String(id)) found = p; });
    return !!found && !valid(found.phone);
  }

  return { valid: valid, problem: problem, required: required, REQUIRED: REQUIRED, PATIENT_NO_PHONE: PATIENT_NO_PHONE };
})();

if (typeof module !== "undefined" && module.exports) module.exports = window.BookingPhone;
