# Security Policy

## Threat model

meeting-guard is designed for this situation, and only this one:

- **A single-user Mac.** It runs as your user (a launchd agent) every minute.
  Other local users and other people with access to your account are out of
  scope.
- **Your own files are trusted.** `config.json`, `secrets.env`, `tokens.json`,
  the OAuth client JSON and everything in `MEETING_GUARD_HOME` are trusted
  when they are owned by you and pass the private-file / directory checks
  below. Their *content* is still validated (type / range checks, fail
  closed for the call ledger), because a broken value must not silently stop
  the alarms.
- **`$HOME` is not shared** with other users. `install.sh` / `uninstall.sh`
  defend against mistakes (bad `MEETING_GUARD_HOME`, symlinks, control
  characters in paths), not against another user racing them inside your
  home directory.
- **All data coming from Google is untrusted**: event titles, descriptions,
  locations, conference data, attendee lists, time zones and page tokens can be
  written by anyone who can invite you or edit a shared event. It is sanitized,
  validated and never executed.
- Malware already running as your user can read every file listed below; this
  tool does not defend against a compromised account.

The guiding rule for robustness: **anything that could silently stop the
alarms is either fixed or announced locally** (voice + an OK-only dialog).

## Known limitations

Each item is tagged **(by design)** or **(PRs welcome)**.

- **Shared calendars trust their owners. (by design)** "Organizer / creator is
  me" (`organizer.self` / `creator.self`) and "no attendee list" are treated as
  your own events. If you add someone else's calendar to `calendars`, its
  owner effectively decides which events alert you, open links and (if
  enabled) trigger a phone call. Use only your own calendars.
  `include_unaccepted: false` filters by *your* attendee response only; it does
  not protect against events on a calendar you subscribed to.
- **State retention and dedupe windows. (by design)** Per-event state and
  acknowledgements are kept while the stored start is within `nag_minutes`
  + 30 minutes (40 minutes by default); the call ledger keeps attempts for 30
  days. The "never alert / call twice" guarantees hold only inside those
  windows.
- **Google Meet attendance cannot be detected. (PRs welcome)** Meet runs in
  the browser; meeting-guard keeps nagging until you click the "joined" button.
  Zoom detection relies on the `CptHost` process, which Zoom may change.
- **Back-to-back Zoom meetings with `auto_open: false`. (PRs welcome)** When
  meeting-guard did not open the link itself (`auto_open` off, or opening
  failed), a `CptHost` started at or after "start - 2 min" counts as joined.
  If the previous Zoom meeting's `CptHost` was started inside that window
  (for example a meeting that began late and runs into the next one), the
  next meeting is treated as joined and its nags / call stop. With
  `auto_open` on and a successful open, meeting-guard records the `CptHost`
  processes running just before it opens the link; those never count as
  joining, only a process started after the open does.
- **Joining a Zoom meeting before the auto-open. (by design)** With
  `auto_open` on, if you joined the meeting manually before meeting-guard
  opened the link, your `CptHost` is in the "already running" record and the
  nags continue until you press the "joined" button. The same happens when
  that record could not be taken or is unreadable. This errs on the side of
  alerting.
- **A Zoom process starting in the second before the auto-open. (PRs
  welcome)** The record of already-running `CptHost` processes is taken just
  before the link is opened. If the previous meeting's `CptHost` starts in
  the moment between that record and the open (about a second), it is
  treated as joining this meeting and the nags / call stop. Zoom offers no
  local way to tell which meeting a process belongs to.
- **No alarms while the Mac sleeps. (by design)** After wake, only the most
  urgent pending stage fires once; an event first seen more than
  `nag_minutes` after its start is skipped (logged once).
- **Paging cap. (PRs welcome)** At most 5 pages (30 events each) are read per
  calendar per run; overflow is logged and announced locally, but events beyond
  it are not alerted.
- **OAuth "Testing" status. (by design, Google policy)** Refresh tokens of an
  OAuth app in Testing status expire after 7 days; `invalid_grant` is
  announced locally. Publish the app ("In production"; unverified is fine for
  personal use) or re-run `--auth`.

## Reporting a vulnerability

Please report security issues privately with GitHub **private vulnerability
reporting** ("Report a vulnerability" under the Security tab of this
repository). Do not open a public issue for security problems.
Include steps to reproduce, the affected version or commit, and the impact.
Please do not include real tokens, phone numbers or calendar data in reports.

## Protections in place

The process sets `umask 077` and the launchd plist sets `Umask` 63 (0o077),
so files it creates are readable by you only.

| Asset | Where | Protection |
|---|---|---|
| Google refresh token | `~/.config/meeting-guard/tokens.json` | only `refresh_token` (+ `scope`) stored; with `token_profile`, the merge runs under `tokens.json.lock`, re-reads the latest file, then writes a temp file in the same directory, fsync, atomic replace, mode 0600; parent directory chmod 0700; scope is `calendar.events.readonly` |
| OAuth client JSON | `~/.config/meeting-guard/gcp-oauth.keys.json` | private-file check on every read (see below) |
| Twilio credentials, phone numbers, ntfy topic / token | `~/.config/meeting-guard/secrets.env` or environment variables | private-file check; values never logged; phone numbers masked to the last 4 digits; an environment variable set to `""` disables the value |
| Call ledger | `$MEETING_GUARD_HOME/calls.json` | private-file check; NaN / Infinity and timestamps outside 2000-01-01 .. now + 1 day are refused (fail closed); written atomically (temp + fsync + replace) before every Twilio request |
| State, acks, locks, logs | `$MEETING_GUARD_HOME/` | the directory must be owned by you, not group/world writable and not a symlink (checked at startup before any write, including config-error logging, `--dialog` ack, `--simulate` and `--test-call`; otherwise stderr + local alarm + exit with no writes); `.lock`, lock files, `state.json`, `acks.txt` and `logs/guard.log` are opened with `O_NOFOLLOW` |
| Logs | `$MEETING_GUARD_HOME/logs/guard.log` | URLs replaced by `[url]`, digit runs of 6+ by `[num]`; exceptions logged as class name + HTTP status only; a log write failure never stops an alarm (falls back to stderr) |

Private-file check (secrets.env, tokens.json, OAuth client JSON, calls.json):
`lstat` must show a regular file (not a symlink) owned by the current user
(`st_uid == getuid()`) with no group/other permission bits; the file is then
opened with `O_NOFOLLOW` and its inode compared. Otherwise the file is refused
and a warning is logged.

Configuration: known keys are type / range checked on every start (booleans
must be JSON `true` / `false`, numbers finite and in range, lists contain only
strings). An invalid key falls back to its default, invalid JSON falls back to
all defaults; in both cases phone calls are disabled and a local alarm is
shown at most hourly (rate-limit file `.alert-config`).

Untrusted input (Google data):

- **Calendar invitations** can be sent by anyone. By default only events you
  organized or created, invitations whose `responseStatus` for you is
  `accepted`, and events without an attendee list are processed
  (`include_unaccepted: false`). Declined events are always ignored. We also
  recommend the Google Calendar setting "Add invitations to my calendar: only
  if the sender is known".
- **Event titles** are sanitized before use: control characters and Unicode
  format characters (category Cf) are removed, `[[` / `]]` (`say` embedded
  commands) are removed, leading `-` are stripped repeatedly, and the result is
  truncated to 80 characters. `say` is run as
  `/usr/bin/say [-v voice] -- <text>`; AppleScript strings are escaped and
  TwiML is XML-escaped. Commands are always run with argument lists, never a
  shell. `exclude_summary_contains` is a list of plain, case-insensitive
  substrings (no regular expressions) matched against the sanitized title.
  Each event is processed inside its own error handler (including the target
  filter).
- **Times**: a `dateTime` without an offset is interpreted in the event's
  `start.timeZone` (via `zoneinfo`); an unknown or malformed zone falls back to
  local time. Non-string values are rejected for that event only.
- **Links in events** are searched in trust order: conferenceData entry points
  of type `video`, then `hangoutLink`, then `location`, then `description`
  (HTML entities decoded first; the first source containing a valid link wins,
  so text in the description cannot override the real conference link). They
  are parsed and rebuilt: https only, no userinfo or port; digits are ASCII
  `[0-9]` only; Zoom host must be `zoom.us` or `*.zoom.us` and the path exactly
  `/j/<digits>` or `/w/<digits>` (opened as `https://<host>/j/<digits>`, with
  `?pwd=` kept only when it is `[A-Za-z0-9._-]`); Meet must be
  `https://meet.google.com/<xxx-yyyy-zzz>`. Only the rebuilt URL (or the
  `zoommtg://` URL derived from it) is passed to `open`. If `open` fails, the
  user is told to join manually (voice + dialog).
- **Volume**: at most `max_events_per_run` (default 3) events produce alerts in
  one run, nearest start first; an event whose actions started counts even if
  it then raised. Calendar listing follows `nextPageToken` for at most 5 pages.
- **Per-calendar isolation**: a calendar that fails to load does not stop the
  others; the failure is announced locally (at most hourly).

State: per-event state and acknowledgements are keyed by
`calendarId|eventId`; the start time is stored in the value and used only for
pruning. Moving an event does not reset what already fired, whether a call was
attempted, or an acknowledgement. Exception: an event recorded as skipped
(`skipped_at_start`, first seen beyond the nag window and never alerted) is
re-armed when its start changes while the event is in the alert window (the
new start is upcoming, or already passed but within `nag_minutes`; the latter
is caught up immediately: open, nag / call check, start-time voice for
events without a link). A legacy `skipped: true` flag is not honoured.

Zoom join detection: start times come from `ps -o lstart=` (the absolute
start time kept by the OS). When meeting-guard opened the link, the
`CptHost` processes running just before the open are recorded; a process
counts as joining only if it is not in that record and started at or after
"open0 fire time - 60 s". If the record is missing or unreadable, nagging
continues. When meeting-guard did not open the link, a process started at or
after "start - 2 min" counts (see Known limitations for back-to-back
meetings). Any detection failure (pgrep / ps error or unexpected output)
keeps nagging; it is never read as "Zoom not running".

Network:

- Authenticated requests (Google token / Calendar, Twilio, ntfy) use an opener
  that refuses redirects and ignores environment proxy variables
  (`ProxyHandler({})`), so credentials are never re-sent to another URL or
  handed to a proxy chosen by the environment (including for localhost).
- When the calendar cannot be read (network or auth error, missing or
  insecure token / client files) a local voice alert and an OK-only dialog
  (which never acknowledges a meeting) are shown at most once per hour; the
  timestamp is persisted in `state.json` (`_meta.last_error_alert`). Push is
  sent too when configured. Google's `invalid_grant` gets a dedicated
  "run `--auth`" message.
- `ntfy_server` must be `https://` (plain `http://` only for `127.0.0.1` /
  `localhost`). `NTFY_TOKEN` is sent as `Authorization: Bearer`.

OAuth (`--auth`):

- Installed-app loopback flow on `http://127.0.0.1:<random port>` with PKCE
  (S256) and a random `state`. Requests to paths other than `/` get 404.
  A `state` that is missing, not a string, non-ASCII, longer than 128
  characters or wrong is ignored (the flow keeps waiting), as is a malformed
  `code`; nothing in the query can crash the flow. The whole flow has an
  absolute 300 s deadline: each connection's socket timeout is min(10 s, time
  left), the handler ignores a code that arrives after the deadline (HTTP
  408), and the wait loop exits at the deadline.
- There are no calendar-writing commands. `--simulate` fires every stage
  locally for a fake event and never places a call.

Phone calls (Twilio, **disabled by default**):

- Local alarms (voice, dialog, push) always run before the call path, and any
  exception in the call path is caught and logged without affecting them. Any
  exception while preparing a call (secrets, lock file, ledger) results in
  "refused": nothing is sent.
- Every attempt is first recorded in `calls.json` (key `calendarId|eventId`).
  A call is refused when the event already has an attempt (no retries, even
  after failure or rescheduling), when `call.max_per_day` (default 2) attempts
  happened in the last 24 hours, or when the last attempt was less than
  `call.min_interval_min` (default 30) minutes ago. If the ledger cannot be
  read, is malformed, insecure, or cannot be written, the call is refused
  (fail closed). `--test-call` goes through the same ledger.
- The event is marked as called only when an attempt exists in the ledger
  (placed, failed, or already recorded). A refusal caused only by
  `max_per_day` / `min_interval_min` (or by missing settings / an unusable
  ledger) records nothing, so a later run inside the nag window may call once
  the limit allows; an unusable ledger keeps refusing.
- After wake from sleep, an event first seen after its start but within
  `nag_minutes` is opened and the call check runs in the same pass
  (respecting `call.after_min`). Events first seen later than that are skipped
  and logged once.
- Numbers must fully match `\+[1-9][0-9]{7,14}` (ASCII digits only, no
  trailing newline; all validators use `re.fullmatch`); optional `call.allowed_to_prefix` /
  `call.allowed_from_prefix`; example placeholder values are refused.
- An API key (`TWILIO_API_KEY_SID` / `TWILIO_API_KEY_SECRET`) is preferred over
  the account auth token for Basic auth.
- The spoken text omits the event title unless `call_include_summary` is true
  (Twilio stores TwiML in its logs).

Install / uninstall:

- `install.sh` validates `MEETING_GUARD_HOME` before any `chmod` or copy: it
  must be an absolute path inside `$HOME`, not `$HOME` itself, contain no
  control characters (newline, tab ...), no component starting with `-`, no
  `.`/`..` components, not be (or pass through) a symlink, and be absent, empty
  or already carry the `.meeting-guard-home` marker. It writes that marker. A
  symlinked `logs/`, plist or `plist.bak` is refused. The plist is generated
  with `plistlib`; the previous plist is copied to a fresh temp file and
  renamed to `.bak`, and the new one is written to a temp file and moved into
  place.
- `uninstall.sh --purge` only lists what it would delete; `--purge --yes`
  deletes a directory only if it passes the same path checks, its resolved
  real path is strictly under the resolved `$HOME`, and it contains the marker.
  Targets are held in a bash array (never split on newlines) and every check
  is repeated right before each `rm -rf --`. It never deletes `/`, `$HOME`,
  paths outside `$HOME` or symlinks.

Known risks you should be aware of:

- **ntfy topic = password.** On the public ntfy.sh server anyone who knows the
  topic can read your notifications and send fake ones. Notification titles and
  bodies (meeting titles by default; see `push_include_summary`) are sent to
  the ntfy server. Push is off until you set a topic.
- **Twilio costs money.** A leaked Twilio credential lets others make calls
  billed to you. Use an API key, restrict Voice Geographic Permissions to your
  own country, and set a usage trigger / spend alert.
