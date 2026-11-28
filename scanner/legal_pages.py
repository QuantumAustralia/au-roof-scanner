"""Public legal pages for the QMS Roof Scan marketplace."""

from __future__ import annotations

from scanner.compliance import AUTOMATED_DECISION

PAGES = {
    "data-terms": (
        "Data use and compliance",
        """
        <h2>Data use and compliance notice</h2>
        <ol>
          <li><strong>Nature of Data &amp; Attribution:</strong> The property addresses, aerial roof assessments, and associated geospatial records supplied through this marketplace constitute property-level intelligence compiled from publicly accessible sources and licensed spatial datasets, including New South Wales spatial data (© State of New South Wales (DCS Spatial Services), licensed under CC BY 4.0). Records do not include homeowner names, telephone numbers, or email addresses.</li>
          <li><strong>Privacy &amp; Outreach Compliance:</strong> By purchasing and using these records, the Purchaser acknowledges that residential address data may constitute "personal information" under the Privacy Act 1988 (Cth) and warrants that it will handle, store, and use the data strictly in accordance with the Australian Privacy Principles (including APP 7 direct-marketing opt-out rules), the Do Not Call Register Act 2006 (Cth), the Spam Act 2003 (Cth), and all applicable NSW Fair Trading contractor licensing requirements. Any outreach or marketing conducted by the Purchaser using these records is undertaken solely at the Purchaser's own responsibility.</li>
          <li><strong>No Resale &amp; Warranty Disclaimer:</strong> The Purchaser is granted a non-transferable, single-business licence to use these records for its own contracting operations and must not resell, redistribute, or sublicence the records to any third party. To the maximum extent permitted by the Australian Consumer Law, the records are provided "as is" without warranty of absolute accuracy or completeness, and the Supplier's aggregate liability arising out of or in connection with an order is limited to the fees paid for the relevant records.</li>
          <li><strong>Proportionate Indemnity:</strong> To the extent permitted by law, the Purchaser indemnifies the Supplier against third-party claims, losses, and reasonable legal costs to the extent caused by the Purchaser's breach of this Notice or unlawful marketing and outreach practices.</li>
        </ol>
        """,
    ),
    "privacy": (
        "Privacy policy",
        f"""
        <h2>Privacy policy</h2>
        <p>QMS Roof Scan supplies property-level roof intelligence to roofing businesses. This policy explains how we handle personal information under the Australian Privacy Principles in the Privacy Act 1988 (Cth).</p>
        <h2>What we hold</h2>
        <p>For a purchasing business we hold the name, business name, phone number, email address, and billing address entered at checkout, together with the order, the consent record, and the technical details of that checkout (time, IP address, and browser). Property records identify an address and a roof assessment. They do not include the homeowner's name, telephone number, or email address.</p>
        <h2>Why we hold it</h2>
        <p>We use buyer details to take payment, issue the invoice, and send the purchased addresses. We use property records to publish listings and to fulfil an order. We do not sell buyer account records.</p>
        <h2>Direct marketing and opt-out</h2>
        <p>Purchasers who contact households must follow APP 7, including a working opt-out. A property owner, or a purchaser acting on a request, may ask us to suppress an address. Suppressed addresses are removed from the public marketplace. Send the suppression request to the contact on your invoice and include the address, suburb, and postcode.</p>
        <h2>Automated decisions</h2>
        <p>{AUTOMATED_DECISION}</p>
        <h2>Storage and disclosure</h2>
        <p>Orders and consent records are stored for the operation of the marketplace and may be held by our payment provider, Stripe, and by our database host when that host is connected. We disclose personal information where the law requires it. We do not disclose homeowner contact details, because we do not hold them.</p>
        <h2>Access and correction</h2>
        <p>A purchaser may ask for the account and order records we hold about their business, and may ask us to correct them. Contact the address on your invoice.</p>
        """,
    ),
    "terms": (
        "Terms of use",
        """
        <h2>Terms of use</h2>
        <p>These terms are a business-to-business agreement for the QMS Roof Scan marketplace. By ticking the consent box and paying, the purchaser agrees to these terms, the privacy policy, and the data use and compliance notice.</p>
        <h2>The marketplace</h2>
        <p>Listings show a suburb, postcode, roof image, and an indicative condition. The street address is sent only after payment. Each purchased lead includes the property address, a roof image file, and a CSV file of the purchased addresses. Prices shown on the marketplace are in Australian dollars and are exclusive of GST. The invoice adds GST at 10 percent, and the amount payable is that GST-inclusive total.</p>
        <h2>Minimum order</h2>
        <p>The standard minimum order is 10 leads. The number shown on the marketplace and at checkout is the minimum that applies to that order, including where a lower number is shown for testing. Payment is refused until that minimum is met.</p>
        <h2>Territory exclusivity</h2>
        <p>After payment, the purchased property record is removed from the public marketplace and is not offered to another buyer for 12 months.</p>
        <h2>Licensing</h2>
        <p>The purchaser warrants that it holds every NSW Fair Trading contractor licence required for the work it offers to households using these records.</p>
        <h2>Use of the records</h2>
        <p>The records are licensed to one business. They must not be resold or passed on. Outreach using the records must comply with the Privacy Act 1988 (Cth), the Spam Act 2003 (Cth), and the Do Not Call Register Act 2006 (Cth).</p>
        <h2>Imagery and condition</h2>
        <p>There are no warranties or guarantees as to the accuracy of the images or the condition of the roofs. Images are third-party satellite imagery and we cannot guarantee the age, currency, or accuracy of any image. Leads are provided for information purposes only and buyers should conduct their own inspections.</p>
        """,
    ),
}


def legal_page(slug: str) -> tuple[str, str] | None:
    """Title and HTML body for a public legal page."""
    found = PAGES.get(slug)
    if found is None:
        return None
    return found
