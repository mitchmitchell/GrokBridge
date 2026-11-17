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
| chat_direct_from_tablet.xml, chat_direct_from_phone.xml | direct chats to GROK-1 |
| chat_all_rooms.xml | All Chat Rooms message (not for the bot) |
| groklink_request_text_only.xml | request from the GrokLink plugin |
| marker_first.xml, marker_update.xml | a-u-G marker, first send and an update |
| route.xml | b-m-r route; five link points, waypoints CP1, CP2, VDO |
| range_bearing.xml | u-rb-a range and bearing line (179.1 m at 115.5 deg) |
| circle.xml | u-d-c-c circle (radius 513.3 m) |
| rectangle.xml | u-d-r rectangle; four corner links without relation |

Coordinates in route, range_bearing, circle and rectangle were moved so the
first position sits at 35, -90 while distances between points stay true.
The server's t-x-d-d notice turned out to mean "client disconnected".
