from __future__ import annotations

import re

from bs4 import BeautifulSoup

from parentsquare_mcp.models import PaymentItem, PaymentPost, PaymentSummary


def parse_payments_page(soup: BeautifulSoup) -> PaymentSummary:
    """Parse /schools/{id}/feeds/payment_feeds -> PaymentSummary.

    Payment page structure:
      div.stat-well — summary stats (Upcoming, Paid For, $ Paid)
      div.ps-accordion-group — one per payment post, containing:
        div#feed_{id}_extras
        .subject > a — post title
        .user-name — author
        div.wish-list-item-row — payment items with .wish-list-item-name + .wish-list-item-price
    """
    # Summary stats
    upcoming = 0
    paid_for = 0
    total_paid = ""
    for item in soup.find_all("div", class_="stat-line-item"):
        number_el = item.find("div", class_="stat-number")
        detail_el = item.find("div", class_="stat-detail")
        if not number_el or not detail_el:
            continue
        detail = detail_el.get_text(strip=True).lower()
        value = number_el.get_text(strip=True)
        if "upcoming" in detail:
            upcoming = int(value) if value.isdigit() else 0
        elif "paid for" in detail:
            paid_for = int(value) if value.isdigit() else 0
        elif "paid" in detail:
            total_paid = value

    # Payment posts
    posts: list[PaymentPost] = []
    for acc in soup.find_all("div", class_="ps-accordion-group"):
        # Find feed ID
        feed_div = acc.find("div", id=re.compile(r"^feed_\d+"))
        if not feed_div:
            continue
        feed_id = int(re.search(r"(\d+)", feed_div["id"]).group(1))

        # Title
        subject = acc.find("div", class_="subject")
        link = subject.find("a") if subject else None
        title = link.get_text(strip=True) if link else ""

        # Author
        author_el = acc.find("a", class_="user-name")
        author = author_el.get_text(strip=True) if author_el else ""

        # Date
        time_el = acc.find("span", class_="time-ago")
        date = ""
        if time_el:
            date = time_el.get("data-timestamp", "") or time_el.get_text(strip=True)

        # Payment items — group by volunteer-list sections
        sections: list[tuple[str, list[PaymentItem]]] = []
        for vol_list in acc.find_all("div", class_="volunteer-list"):
            section_name_el = vol_list.find("div", class_="wish-list-section-name")
            section_name = section_name_el.get_text(strip=True) if section_name_el else ""

            items: list[PaymentItem] = []
            for row in vol_list.find_all("div", class_="wish-list-item-row"):
                classes = " ".join(row.get("class", []))
                if "payment-list-head" in classes or "payment-list-total" in classes:
                    continue
                name_el = row.find("div", class_="wish-list-item-name")
                price_el = row.find("div", class_="wish-list-item-price")
                if not name_el or not price_el:
                    continue
                name = name_el.get_text(strip=True)
                price = price_el.get_text(strip=True)
                if name and name != "Item Name":
                    items.append(PaymentItem(name=name, price=price))

            if items:
                sections.append((section_name, items))

        posts.append(PaymentPost(
            id=feed_id,
            title=title,
            author=author,
            date=date,
            sections=sections,
        ))

    return PaymentSummary(
        upcoming=upcoming,
        paid_for=paid_for,
        total_paid=total_paid,
        posts=posts,
    )
