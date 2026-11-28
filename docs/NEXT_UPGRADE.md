# EffluentIQ — Recommendations for the next upgrade

Written 27 Nov 2026, after Rev 1.2.73 went live. That release added the Alarm notifications panel, critical alarms,
escalation, the weekly report, Send report now, the monthly PDF compliance summary, the settings Change history,
settings backup and restore, optional Twilio texts, the `/healthz` check, and hide/show boxes.

---

## 1. First, check that this release is working

Do these in the first week. None of them needs new code.

- [ ] **Settings → Alarm notifications → Send test email.** It should arrive from **EffluentIQ Alerts &lt;alarm@octaveworks.com&gt;** and land in the inbox, not spam.
- [ ] **Send test text** to each phone. If texts are late or missing, see Twilio under section 3.
- [ ] **Settings → Alarm notification schedule → Critical alarms.** Check the ticks. The defaults are pH, EQ tank high level, E-Stop and EQ tank FULL.
- [ ] **Escalation.** Turn it on and add a supervisor if you want it.
- [ ] **Email reports → Report recipients.** Fill this in if reports should go to different people from the alarm emails.
- [ ] **Send report now → Monthly.** Check the PDF compliance summary attached to it.

---

## 2. Recommended for the next upgrade (most useful first)

| # | Change | Why | Size |
|---|---|---|---|
| 1 | **The preview copy uses the live settings and logins.** `stage.sh` would copy the live settings and accounts into the test database. | Today the preview starts empty, so you have to set a new Admin password and the limits and recipients are blank. You would check the new version with real settings. | Small |
| 2 | **The PDF and Excel compliance reports use the pH limits that were in force on each day.** | Today they use the limits in Settings when the report is made. If the limits change, older months would be judged against the new limits, which matters for an inspector. The Change history now records every limit change, so the old values are available. | Medium |
| 3 | **"I'm on it" acknowledgement from the email or text.** A link in the alarm email records who is handling the alarm and stops the escalation and "still active" follow-ups. | Alarms are acknowledged automatically, so escalation is based on time only. It would also go out when someone is already fixing the problem. | Medium |
| 4 | **A web address and a proper HTTPS certificate,** for example `dashboard.octaveworks.com` with a free Let's Encrypt certificate. | The browser stops showing a security warning. An outside uptime monitor can check `/healthz` without errors. Links in emails look trustworthy. Needs a DNS record for octaveworks.com. | Small–medium |
| 5 | **Check the live Total Flow reading.** When live readings started (24 Nov), live Total_Flow_Li read 0.0 while the history showed about 10,158 L. | Check with the PLC program whether the totaliser resets, so daily volumes and cost per m³ can be trusted. Investigation first, then a fix only if needed. | Small |
| 6 | **Choose which reports each person gets,** for example a manager monthly only and an operator daily only. | Today everyone on the report list gets every report that is turned on. | Small |
| 7 | **Two escalation levels:** a supervisor after 30 min and a manager after 2 h. | Larger problems reach the right person without waking everyone at the first step. | Small |
| 8 | **Phone layout check** for Settings (three cards side by side) and the wide tables. | Operators often check from a phone. | Small |
| 9 | **Fix the flaky test** `test_engineering_guest_logins_and_rights`. It sometimes fails because an Operator login from another test is left behind. | The release test run should never fail for no reason. | Small |

---

## 3. Setup outside the code (no upgrade needed)

- **Twilio SMS (optional).** Do this if carrier email-to-text is unreliable.
  1. Create a Twilio account and buy a number.
  2. Put the account details in `/etc/pbf-wastewater/sms.json` on the server: `{"account_sid": "AC…", "auth_token": "…", "from_number": "+1…"}`.
  3. Choose **Twilio SMS** under Settings → Text message alerts → Send texts by.

  Texts cost a few cents each.
- **Uptime monitor.** Point a free monitor (for example UptimeRobot) at `https://<server>/healthz`. It answers 200 when everything is fine and 503 otherwise. Do this after #4 above, or turn off the monitor's certificate check until then.
- **Email security (DMARC).** octaveworks.com has SPF, DKIM and DMARC set up. DMARC is `p=none` (monitoring only). After a few weeks of alarms and reports arriving normally, consider `p=quarantine`, with a report address (`rua=`) so you can see who sends as your domain.

---

## 4. Ideas for later

- **A view of several plants on one page** for a chemical supplier (stock, order-now and cost per m³ for all their sites). This fits the partner idea discussed with CESCO.
- **Reorder email to the supplier** when a chemical reaches ORDER NOW, with the quantity and PO number filled in.

---

### How to release (reminder)

1. Run `.\release.cmd`.
2. At step 4, if you can't sign in to the preview, set a preview-only Admin password in a second window:
   `ssh -t ubuntu@51.161.52.12 "sudo -u pbf-wastewater env PBF_APP_DB=/var/lib/pbf-wastewater/dashboard-vX.Y.Z-stage.sqlite3 python3 /opt/pbf-wastewater/revX.Y.Z-stage/web.py --set-admin admin"`
   Replace X.Y.Z with the new version number. Recommendation #1 would make this unnecessary.
3. Check the preview at http://localhost:8003, then type **DEPLOY** to go live.
