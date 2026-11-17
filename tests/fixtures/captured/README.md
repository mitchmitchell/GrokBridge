Real events captured from the live TAK Server (5.8-RELEASE-103-HEAD) on
zodiac by GROK-1's connection, 2026-11-17. These are the source of truth for
message formats (ground rule 3).

Sanitized with tools/sanitize.py before committing: coordinates set to
35, -90; device uids, the WinTAK Windows SID, the callsign (TEST1/TAK),
phone numbers and the server node id replaced. Structure is unchanged.

| File | What it is |
| --- | --- |
| position_atak_tablet.xml | ATAK-CIV 5.8.0.10 (developer build) position; callsign has a trailing space |
| position_atak_phone.xml | ATAK-CIV 5.6.0.12 (Play Store) position |
| position_wintak.xml | WinTAK-CIV 5.8.0.150 position, type a-f-G-U-C-I |
| server_protocol_offer.xml | t-x-takp-v: server offers TAK protocol v1 (ignored; we stay on XML) |
| server_t-x-d-d.xml | t-x-d-d naming takbridge-GROK-1 |
| server_t-x-g-c.xml | t-x-g-c with an empty p-p link |
