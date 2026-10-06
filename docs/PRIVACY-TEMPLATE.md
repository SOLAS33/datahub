# Privacy page template for Solas33 sites

Use this structure for every site's `/privacy` page (it is implemented in the templates of FirmWatch, TenderWatch, GridWatch and DCWatch). Keep the wording plain (see WRITING.md). Replace `{SITE}` and delete sections the site does not need.

## Company block (same on every site, also in the footer)

> {SITE} is operated by **SOLAS33 Atelier Limited**, a company registered in Ireland (company number 826434) whose registered office is 11 Grattan Road, Claddagh, Galway, Ireland. It decides why and how the personal data below is used, which makes it the "controller". For anything in this notice, including requests about your data, contact hello@solas33.com or write to the registered office.

Footer line: `SOLAS33 Atelier Limited, registered in Ireland (company number 826434). Registered office: 11 Grattan Road, Claddagh, Galway, Ireland.`

## Sections, in this order

1. **Short version.** Three sentences: what is collected when browsing (nothing), when subscribing, when writing to us.
2. **Who is responsible.** The company block above.
3. **Browsing the site and the free tools.** No cookies, no analytics or ad trackers, no third-party scripts. Theme choice stays in the browser. State which tools run in the browser and which files they fetch from data.solas33.com. Name Cloudflare as host.
4. **If you subscribe.** Table: what we store, why and our basis (consent, double opt-in), how long, abuse protection.
5. **If you use a watchlist** (only sites with one). What is stored, that it reveals an interest and is used only for alerts, deleted with the subscription.
6. **If you send an enquiry or order a report.** Table: what we store, basis (steps before a contract, then the contract; legitimate interest), retention.
7. **Who processes it for us.** Cloudflare (hosting, database) and Brevo (email). No sale of data. International transfers under standard contractual clauses.
8. **Your rights.** Access, correction, export, deletion, restriction, objection, withdrawal of consent. Reply within one month. Right to complain to the Data Protection Commission (dataprotection.ie).
9. **The public data.** What personal data the source data might contain, what the hub drops at collection, and how to ask for a review.
10. **Last reviewed** date.

## Retention periods in force (all sites)

| Data | Kept for |
|---|---|
| Unconfirmed sign-up (and its watches) | 7 days, then deleted by the Worker |
| Confirmed subscriber | until they unsubscribe (deleted at once) |
| Sign-up network address (abuse protection) | 24 hours |
| Enquiry | up to 12 months after last contact |
| Certified report and its inputs | 7 years, so it can be re-verified; deleted earlier on request |

## When something changes

Update the template and every site's page together, bump "Last reviewed", and tell subscribers by email if the change affects how their data is used.
