# Salon and wellness industry pack

Set the industry before starting FrontDesk:

```powershell
$env:FRONTDESK_INDUSTRY = "salon"
$env:FRONTDESK_WEB_PERSONA = "salon"
```

This enables salon-only tools without exposing them to ecommerce, hospitality,
or other tenants:

- service, duration, price, location, and stylist catalogue;
- availability search with service duration and overlapping-booking checks;
- explicitly approved appointment creation;
- verified-customer email enforcement;
- multi-location and stylist eligibility;
- email reminder state and retry-safe delivery;
- deterministic human handoff for allergy, reaction, pregnancy, patch-test, and
  chemical-treatment safety questions;
- responsive upcoming-appointments view in FrontDesk Admin.

The included catalogue is demonstration data. Replace it through the customer
booking backend before production. FrontDesk uses these tenant-scoped endpoints:

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/services` | Locations, services, duration, price, and staff |
| `GET` | `/availability` | Slots filtered by date, service, staff, and location |
| `POST` | `/appointments` | Create one appointment with an `Idempotency-Key` |
| `GET` | `/reservations` | Search existing appointments |
| `PATCH` | `/reservations/{id}` | Change an appointment |
| `DELETE` | `/reservations/{id}` | Cancel an appointment |

Use `FRONTDESK_TENANT_BACKENDS_FILE` for multiple salons. Keep each backend token
in the environment variable named by that tenant profile; never place the token
inside the JSON profile or customer documentation.

## Email reminders

Configure the tenant's SMTP profile in `FRONTDESK_INTEGRATIONS_FILE`, with the
username and password referenced through environment-variable names. Test without
sending:

```powershell
python salon_reminders.py --tenant salon-a --within-hours 24 --dry-run
```

Then schedule the same command without `--dry-run` in Windows Task Scheduler or
cron. A successful reminder is marked `sent`. A failed SMTP delivery remains
`pending`, so the next scheduled run can retry it without marking a false success.

## Safety and privacy boundary

FrontDesk does not assess whether dye, bleach, adhesive, or another treatment is
medically safe. It asks for a human consultation and does not request or store the
customer's diagnosis, medication, pregnancy details, allergy history, or clinical
notes. The salon remains responsible for its consultation, patch-test, consent,
retention, and emergency procedures.

## Production acceptance

Before advertising live booking, connect the salon's actual booking system and
test service IDs, staff IDs, location time zones, overlapping appointments,
cancellation rules, reminder delivery, provider rate limits, and recovery from a
failed booking request. The included local store proves the workflow, not a
Fresha, Square Appointments, Vagaro, or Mindbody integration.
