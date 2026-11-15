"""Generate five days of fake landing-zone files for a Finnish web shop.

Every file is fake (Faker, fi_FI locale, fixed seed). The demo subject
C00042 is shaped to make erasure hard on purpose:
  * changes email on day 2 and address on day 4, so old values survive
    only in older extracts and in SCD2 history
  * has orders that carry the shipping name and address at order time
  * opened support tickets from both the old and the new email
  * is mentioned by name and phone in a ticket written by someone else
"""
from __future__ import annotations

import csv
import json
import random
import shutil
import unicodedata
from datetime import date, datetime, timedelta

from faker import Faker

from gdpr.config import RAW_DIR

SEED = 7
DAYS = [date(2026, 11, 1) + timedelta(days=i) for i in range(5)]
DEMO_ID = "C00042"
START_CUSTOMERS = 280
NEW_PER_DAY = 5
SEGMENTS = ["new", "regular", "loyal", "vip"]
PAGES = ["/", "/scooters", "/scooters/e-200", "/helmets", "/cart", "/checkout", "/account"]
DOMAINS = ["gmail.com", "outlook.com", "elisanet.fi", "kolumbus.fi", "yahoo.com", "hotmail.fi"]

CUSTOMER_FIELDS = ["customer_id", "first_name", "last_name", "email", "phone", "street_address",
                   "postal_code", "city", "date_of_birth", "marketing_opt_in", "segment", "updated_at"]
TICKET_FIELDS = ["ticket_id", "created_at", "customer_email", "channel", "subject", "body"]


def _ascii(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().replace(" ", "")


class Generator:
    def __init__(self) -> None:
        self.fake = Faker("fi_FI")
        Faker.seed(SEED)
        self.rng = random.Random(SEED)
        self.customers: dict[str, dict] = {}
        self.next_id = 1
        self.order_seq = 1
        self.ticket_seq = 1
        self.event_seq = 1

    # ---- attribute helpers -------------------------------------------------
    def phone(self) -> str:
        r = self.rng
        return f"+358 4{r.choice('0145')} {r.randint(100, 999)} {r.randint(1000, 9999)}"

    def email(self, first: str, last: str) -> str:
        return f"{_ascii(first)}.{_ascii(last)}{self.rng.randint(1, 99)}@{self.rng.choice(DOMAINS)}".lower()

    def address(self) -> tuple[str, str, str]:
        return (self.fake.street_address(), self.fake.postcode(), self.fake.city())

    def new_customer(self, day: date) -> dict:
        cid = f"C{self.next_id:05d}"
        self.next_id += 1
        first, last = self.fake.first_name(), self.fake.last_name()
        street, postcode, city = self.address()
        c = {
            "customer_id": cid, "first_name": first, "last_name": last,
            "email": self.email(first, last), "phone": self.phone(),
            "street_address": street, "postal_code": postcode, "city": city,
            "date_of_birth": self.fake.date_of_birth(minimum_age=18, maximum_age=80).isoformat(),
            "marketing_opt_in": self.rng.random() < 0.4,
            "segment": self.rng.choice(SEGMENTS[:2]), "updated_at": f"{day}T06:00:00",
        }
        self.customers[cid] = c
        return c

    def mutate(self, c: dict, day: date, kind: str | None = None) -> None:
        kind = kind or self.rng.choice(["email", "address", "segment", "phone", "opt_in"])
        if kind == "email":
            c["email"] = self.email(c["first_name"], c["last_name"])
        elif kind == "address":
            c["street_address"], c["postal_code"], c["city"] = self.address()
        elif kind == "segment":
            idx = SEGMENTS.index(c["segment"])
            c["segment"] = SEGMENTS[min(idx + 1, len(SEGMENTS) - 1)]
        elif kind == "phone":
            c["phone"] = self.phone()
        else:
            c["marketing_opt_in"] = not c["marketing_opt_in"]
        c["updated_at"] = f"{day}T{self.rng.randint(7, 21):02d}:{self.rng.randint(0, 59):02d}:00"

    # ---- record builders ---------------------------------------------------
    def order(self, c: dict, day: date) -> dict:
        oid = f"O{self.order_seq:06d}"
        self.order_seq += 1
        return {
            "order_id": oid, "customer_id": c["customer_id"],
            "order_ts": f"{day}T{self.rng.randint(8, 22):02d}:{self.rng.randint(0, 59):02d}:00",
            "amount_eur": round(self.rng.uniform(19, 1499), 2),
            "items": self.rng.randint(1, 4),
            "customer_email": c["email"],
            "shipping_name": f"{c['first_name']} {c['last_name']}",
            "shipping_address": f"{c['street_address']}, {c['postal_code']} {c['city']}",
        }

    def ticket(self, email: str, day: date, subject: str, body: str) -> dict:
        tid = f"T{self.ticket_seq:05d}"
        self.ticket_seq += 1
        return {"ticket_id": tid, "created_at": f"{day}T{self.rng.randint(8, 18):02d}:15:00",
                "customer_email": email, "channel": self.rng.choice(["email", "chat", "form"]),
                "subject": subject, "body": body}

    def event(self, cid: str | None, day: date) -> dict:
        eid = f"E{self.event_seq:07d}"
        self.event_seq += 1
        return {"event_id": eid, "customer_id": cid,
                "event_ts": f"{day}T{self.rng.randint(0, 23):02d}:{self.rng.randint(0, 59):02d}:{self.rng.randint(0, 59):02d}",
                "page": self.rng.choice(PAGES), "ip_address": self.fake.ipv4_public(),
                "user_agent": self.fake.user_agent()}

    # ---- main loop ---------------------------------------------------------
    def run(self) -> None:
        if RAW_DIR.exists():
            shutil.rmtree(RAW_DIR)
        for sub in ("crm", "shop", "support", "web"):
            (RAW_DIR / sub).mkdir(parents=True)

        for _ in range(START_CUSTOMERS):
            self.new_customer(DAYS[0])
        demo = self.customers[DEMO_ID]
        demo["segment"] = "regular"
        family = self.customers["C00117"]

        complaints = [
            ("Delivery delayed", "My order has not arrived yet. Can you check the status?"),
            ("Wrong size helmet", "I received size M but ordered L. How do I exchange it?"),
            ("Battery issue", "The scooter battery drains in two days. Is this covered by warranty?"),
            ("Invoice copy", "Please send a copy of my invoice for the last order."),
        ]

        for i, day in enumerate(DAYS):
            if i > 0:
                for _ in range(NEW_PER_DAY):
                    self.new_customer(day)
                for c in self.rng.sample(list(self.customers.values()), k=int(len(self.customers) * 0.06)):
                    if c["customer_id"] != DEMO_ID:
                        self.mutate(c, day)

            orders, tickets, events = [], [], []

            # demo subject storyline
            if i == 0:
                orders.append(self.order(demo, day))
                tickets.append(self.ticket(demo["email"], day, "Question about my order",
                                           f"Hi, this is {demo['first_name']} {demo['last_name']}. "
                                           f"When will order {orders[-1]['order_id']} ship? Call me on {demo['phone']}."))
            if i == 1:
                self.mutate(demo, day, "email")
                orders.append(self.order(demo, day))
            if i == 2:
                demo["segment"] = "loyal"
                demo["updated_at"] = f"{day}T12:30:00"
                tickets.append(self.ticket(family["email"], day, "Order for my partner",
                                           f"Hello, I am writing on behalf of my partner {demo['first_name']} "
                                           f"{demo['last_name']}. Please call them on {demo['phone']} about the refund."))
            if i == 3:
                self.mutate(demo, day, "address")
                orders.append(self.order(demo, day))
                tickets.append(self.ticket(demo["email"], day, "Address change",
                                           "I moved, please use my new address for future deliveries."))
            for _ in range(self.rng.randint(4, 8)):
                events.append(self.event(DEMO_ID, day))

            # background traffic
            pool = list(self.customers.values())
            for c in self.rng.sample(pool, k=55):
                orders.append(self.order(c, day))
            for c in self.rng.sample(pool, k=8):
                subj, body = self.rng.choice(complaints)
                tickets.append(self.ticket(c["email"], day, subj, body))
            for _ in range(350):
                c = self.rng.choice(pool) if self.rng.random() < 0.6 else None
                events.append(self.event(c["customer_id"] if c else None, day))

            # full CRM extract every day, as a nightly export would produce
            with open(RAW_DIR / "crm" / f"customers_{day}.csv", "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=CUSTOMER_FIELDS)
                w.writeheader()
                for c in sorted(self.customers.values(), key=lambda r: r["customer_id"]):
                    w.writerow(c)
            with open(RAW_DIR / "shop" / f"orders_{day}.jsonl", "w") as f:
                for o in sorted(orders, key=lambda r: r["order_ts"]):
                    f.write(json.dumps(o, ensure_ascii=False) + "\n")
            with open(RAW_DIR / "support" / f"tickets_{day}.csv", "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=TICKET_FIELDS)
                w.writeheader()
                w.writerows(tickets)
            with open(RAW_DIR / "web" / f"events_{day}.jsonl", "w") as f:
                for e in sorted(events, key=lambda r: r["event_ts"]):
                    f.write(json.dumps(e) + "\n")


def generate() -> None:
    Generator().run()


if __name__ == "__main__":
    generate()
    print(f"Wrote raw files to {RAW_DIR}  ({datetime.now():%H:%M:%S})")
