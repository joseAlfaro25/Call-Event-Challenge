# Prompt changelog

## v1

Initial version. Closed conversational catalog and explicit distinctions for visit, WhatsApp
consent, wrong person, and do-not-contact.

## v2

Adds structured callback extraction (`callback_day` and `callback_time`) while retaining the
original phrase in `callback_when_raw` for compatibility. Calendar arithmetic remains in code.
