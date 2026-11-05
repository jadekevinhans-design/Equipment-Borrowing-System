"""
cpe_core.py - business logic for the CpE Equipment Borrowing System.

No input() or print() in here. Every rule lives in LabSystem and reports problems
by raising BorrowingError, so the GUI (or a test) decides how to show them.
Data is saved automatically to cpe_data.json after every change.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import ClassVar

# ---------- SETTINGS ----------
DATA_FILE = Path(__file__).with_name("cpe_data.json")
MAX_USERS = 40
MAX_LOG_ENTRIES = 5000
MAX_LOGIN_ATTEMPTS = 3
LOCKOUT_SECONDS = 30
MIN_PASSWORD_LENGTH = 6

ACCESS_CODES = {"PROFESSOR": "PROF-CPE", "CUSTODIAN": "CPE-LAB"}
DEFAULT_ADMIN = ("admin", "admin123")          # change it on first login!

DEFAULT_SETTINGS = {
    "student_days": 3,       # max days a student can borrow
    "professor_days": 7,     # max days a professor can borrow
    "student_max": 3,        # max active requests per student
    "professor_max": 5,      # max active requests per professor
    "penalty": 50,           # PHP per day late (students only)
}
SETTING_MINIMUMS = {"student_days": 1, "professor_days": 1, "student_max": 1,
                    "professor_max": 1, "penalty": 0}

STOCK_STATES = ("Available", "Borrowed", "Under Maintenance", "Defective")
ACTIVE_STATUSES = ("Pending", "Approved", "Borrowed")


REGISTERABLE_ROLES = ("STUDENT", "PROFESSOR", "CUSTODIAN")

DATE_FORMAT = "%Y-%m-%d"
STAMP_FORMAT = "%Y-%m-%d %H:%M:%S"


class BorrowingError(Exception):
    """A rule was broken or an input was invalid. The message is safe to show the user."""


# ---------- SMALL HELPERS ----------
def now_text() -> str:
    return datetime.now().strftime(STAMP_FORMAT)


def to_date(text: str) -> date:
    return datetime.strptime(text, DATE_FORMAT).date()


def parse_int(text: str, label: str = "Value", minimum: int | None = None) -> int:
    text = str(text).strip()
    if not text.lstrip("-").isdigit():
        raise BorrowingError(f"{label} must be a whole number.")
    number = int(text)
    if minimum is not None and number < minimum:
        raise BorrowingError(f"{label} must be at least {minimum}.")
    return number


def parse_date(text: str, label: str = "Date") -> date:
    try:
        return to_date(text.strip())
    except ValueError:
        raise BorrowingError(f"{label} must be a valid date in YYYY-MM-DD format.") from None


def require_text(text: str, label: str) -> str:
    text = str(text).strip()
    if not text:
        raise BorrowingError(f"{label} cannot be empty.")
    return text


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 120_000)
    return f"{salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    salt, _, _ = stored.partition("$")
    return hmac.compare_digest(hash_password(password, salt), stored)


# ---------- MODELS ----------
@dataclass
class Technician:
    name: str
    position: str


@dataclass
class User:
    user_id: str
    username: str
    password_hash: str
    full_name: str
    email: str
    id_no: str

    role: ClassVar[str] = "USER"
    can_borrow: ClassVar[bool] = False
    penalty_exempt: ClassVar[bool] = False

    def borrow_limits(self, settings: dict) -> tuple[int, int]:
        """Returns (max_days, max_active_requests)."""
        raise BorrowingError("This account type cannot borrow equipment.")

    def to_dict(self) -> dict:
        data = asdict(self)
        data["role"] = self.role
        return data


class Student(User):
    role = "STUDENT"
    can_borrow = True

    def borrow_limits(self, settings):
        return settings["student_days"], settings["student_max"]


class Professor(User):
    role = "PROFESSOR"
    can_borrow = True
    penalty_exempt = True

    def borrow_limits(self, settings):
        return settings["professor_days"], settings["professor_max"]


class Custodian(User):
    role = "CUSTODIAN"


class Administrator(User):
    role = "ADMIN"


USER_CLASSES = {c.role: c for c in (Student, Professor, Custodian, Administrator)}


def make_user(role: str, **fields) -> User:
    return USER_CLASSES[role](**fields)


def user_from_dict(data: dict) -> User:
    data = dict(data)
    return make_user(data.pop("role"), **data)


@dataclass
class Equipment:
    equipment_id: str
    name: str
    category: str
    stock: dict

    @classmethod
    def new(cls, equipment_id, name, category, total):
        stock = {state: 0 for state in STOCK_STATES}
        stock["Available"] = total
        return cls(equipment_id, name, category, stock)

    @property
    def total(self) -> int:
        return sum(self.stock.values())

    def check_availability(self, quantity: int) -> bool:
        return self.stock["Available"] >= quantity

    def move(self, from_state: str, to_state: str, quantity: int) -> None:
        self.stock[from_state] -= quantity
        self.stock[to_state] += quantity


@dataclass
class BorrowRequest:
    request_id: str
    username: str
    equipment_id: str
    quantity: int
    date_requested: str
    borrow_date: str
    expected_return_date: str
    status: str = "Pending"
    remarks: str = ""

    def cancel(self) -> bool:
        if self.status in ("Pending", "Approved"):
            self.status = "Cancelled"
            return True
        return False


@dataclass
class Transaction:
    transaction_id: str
    request_id: str
    username: str
    item_name: str
    quantity: int
    date_released: str
    released_by: str
    date_returned: str | None = None
    received_by: str = ""
    condition: str = ""
    penalty: int = 0


@dataclass
class MaintenanceLog:
    log_id: str
    equipment_id: str
    item_name: str
    quantity: int
    state: str
    description: str
    date_recorded: str
    technician: str
    status: str = "Ongoing"


@dataclass
class LogEntry:
    username: str
    action: str
    timestamp: str


# ---------- THE SYSTEM ----------
class LabSystem:
    REPORTS = ("Inventory Report", "Transaction Report", "High-Demand Items",
               "Unreturned / Damaged Items")

    def __init__(self, path: Path | str = DATA_FILE):
        self.path = Path(path)
        self._failed: dict[str, int] = {}
        self._locked_until: dict[str, float] = {}
        self._reset_data()
        if self.path.exists():
            self._load()
        else:
            self._seed()
            self.save()

    # ----- storage -----
    def _reset_data(self):
        self.settings = dict(DEFAULT_SETTINGS)
        self.counters = {k: 0 for k in ("user", "equipment", "request", "transaction", "log")}
        self.users: list[User] = []
        self.technicians: list[Technician] = []
        self.equipment: list[Equipment] = []
        self.requests: list[BorrowRequest] = []
        self.transactions: list[Transaction] = []
        self.maintenance: list[MaintenanceLog] = []
        self.logs: list[LogEntry] = []

    def save(self) -> None:
        data = {
            "settings": self.settings,
            "counters": self.counters,
            "users": [u.to_dict() for u in self.users],
            "technicians": [asdict(t) for t in self.technicians],
            "equipment": [asdict(e) for e in self.equipment],
            "requests": [asdict(r) for r in self.requests],
            "transactions": [asdict(t) for t in self.transactions],
            "maintenance": [asdict(m) for m in self.maintenance],
            "logs": [asdict(l) for l in self.logs],
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)          # atomic: never leaves a half-written file

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.settings.update(data["settings"])
            self.counters.update(data["counters"])
            self.users = [user_from_dict(u) for u in data["users"]]
            self.technicians = [Technician(**t) for t in data["technicians"]]
            self.equipment = [Equipment(**e) for e in data["equipment"]]
            self.requests = [BorrowRequest(**r) for r in data["requests"]]
            self.transactions = [Transaction(**t) for t in data["transactions"]]
            self.maintenance = [MaintenanceLog(**m) for m in data["maintenance"]]
            self.logs = [LogEntry(**l) for l in data["logs"]]
        except (ValueError, KeyError, TypeError):
            # corrupted file: keep a copy and start fresh instead of crashing
            self.path.replace(self.path.with_suffix(".bak"))
            self._reset_data()
            self._seed()
            self.save()

    def _seed(self) -> None:
        self.users.append(Administrator(
            self.next_id("user", "USR"), DEFAULT_ADMIN[0], hash_password(DEFAULT_ADMIN[1]),
            "System Administrator", "admin@bulsu.edu.ph", "N/A"))
        self.technicians += [Technician("Ms. Reyes", "Laboratory Head"),
                             Technician("Mr. Onglao", "Lab Technician"),
                             Technician("Ms. Usigan", "Equipment Custodian")]
        defaults = [
            ("Arduino Uno R3", "Microcontroller", 10), ("ESP32 Dev Board", "Microcontroller", 8),
            ("Raspberry Pi 4", "Single-Board Computer", 6),
            ("FPGA Dev Board (DE10-Lite)", "Digital Design", 4),
            ("Digital Multimeter", "Measuring Tool", 12), ("Oscilloscope", "Measuring Tool", 3),
            ("Logic Analyzer", "Measuring Tool", 4), ("Breadboard", "Prototyping", 20),
            ("Jumper Wire Set", "Prototyping", 25), ("Soldering Iron", "Tools", 8),
            ("LAN Cable Tester", "Networking", 5), ("RJ45 Crimping Tool", "Networking", 5),
            ("DC Power Supply", "Power", 5),
        ]
        for name, category, total in defaults:
            self.equipment.append(Equipment.new(self.next_id("equipment", "EQ"), name, category, total))

    # ----- ids / logs / lookups -----
    def next_id(self, kind: str, prefix: str) -> str:
        self.counters[kind] += 1
        return f"{prefix}-{self.counters[kind]:03d}"

    def log(self, username: str, action: str) -> None:
        self.logs.append(LogEntry(username, action, now_text()))
        if len(self.logs) > MAX_LOG_ENTRIES:
            del self.logs[: len(self.logs) - MAX_LOG_ENTRIES]

    def find_user(self, username: str) -> User | None:
        wanted = username.strip().lower()
        return next((u for u in self.users if u.username.lower() == wanted), None)

    def role_of(self, username: str) -> str:
        user = self.find_user(username)
        return user.role if user else "UNKNOWN"

    def find_equipment(self, equipment_id: str) -> Equipment | None:
        return next((e for e in self.equipment if e.equipment_id == equipment_id), None)

    def find_request(self, request_id: str) -> BorrowRequest:
        for r in self.requests:
            if r.request_id == request_id:
                return r
        raise BorrowingError("That request no longer exists.")

    def find_transaction(self, request_id: str) -> Transaction | None:
        return next((t for t in self.transactions if t.request_id == request_id), None)

    def item_name(self, equipment_id: str) -> str:
        item = self.find_equipment(equipment_id)
        return item.name if item else "(deleted item)"

    @staticmethod
    def _require_role(actor: User, *roles: str) -> None:
        if actor.role not in roles:
            raise BorrowingError("You are not allowed to do that.")

    def _get_equipment(self, equipment_id: str) -> Equipment:
        item = self.find_equipment(equipment_id)
        if item is None:
            raise BorrowingError("That equipment no longer exists.")
        return item

    # ----- accounts -----
    def register(self, role, access_code, username, password, full_name, email, id_no) -> User:
        if role not in REGISTERABLE_ROLES:
            raise BorrowingError("Admin accounts cannot be registered.")
        if len(self.users) >= MAX_USERS:
            raise BorrowingError("User limit reached.")
        if role != "STUDENT":
            if not hmac.compare_digest(access_code.encode(), ACCESS_CODES[role].encode()):
                raise BorrowingError("Wrong access code.")

        username = require_text(username, "Username")
        full_name = require_text(full_name, "Full name")
        id_no = require_text(id_no, "Student/Employee number")
        if self.find_user(username):
            raise BorrowingError("Username already taken. Try another.")
        if len(password) < MIN_PASSWORD_LENGTH:
            raise BorrowingError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email.strip()):
            raise BorrowingError("Please enter a valid email address.")

        user = make_user(role, user_id=self.next_id("user", "USR"), username=username,
                         password_hash=hash_password(password), full_name=full_name,
                         email=email.strip(), id_no=id_no)
        self.users.append(user)
        self.log(username, "REGISTERED: " + role)
        self.save()
        return user

    def login(self, username: str, password: str, role: str) -> User:
        key = username.strip().lower()
        wait = self._locked_until.get(key, 0) - time.time()
        if wait > 0:
            raise BorrowingError(f"Too many attempts. Try again in {int(wait) + 1} seconds.")

        user = self.find_user(username)
        if user is None or not verify_password(password, user.password_hash):
            self._failed[key] = self._failed.get(key, 0) + 1
            self.log("UNKNOWN", "FAILED LOGIN: " + username.strip())
            self.save()
            if self._failed[key] >= MAX_LOGIN_ATTEMPTS:
                self._failed[key] = 0
                self._locked_until[key] = time.time() + LOCKOUT_SECONDS
                raise BorrowingError(f"Too many attempts. Locked for {LOCKOUT_SECONDS} seconds.")
            raise BorrowingError("Invalid username or password.")

        if user.role != role:
            self.log(user.username, "FAILED LOGIN (WRONG ROLE)")
            self.save()
            raise BorrowingError("This account is not registered under the selected role.")

        self._failed.pop(key, None)
        self.log(user.username, "LOGIN")
        self.save()
        return user

    def logout(self, user: User) -> None:
        self.log(user.username, "LOGOUT")
        self.save()

    def uses_default_password(self, user: User) -> bool:
        return (user.username == DEFAULT_ADMIN[0]
                and verify_password(DEFAULT_ADMIN[1], user.password_hash))

    def change_password(self, user: User, old: str, new: str) -> None:
        if not verify_password(old, user.password_hash):
            raise BorrowingError("Current password is incorrect.")
        if len(new) < MIN_PASSWORD_LENGTH:
            raise BorrowingError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
        user.password_hash = hash_password(new)
        self.log(user.username, "CHANGE PASSWORD")
        self.save()

    # ----- lab personnel -----
    def add_technician(self, actor, name, position) -> None:
        self._require_role(actor, "CUSTODIAN")
        name, position = require_text(name, "Name"), require_text(position, "Position")
        self.technicians.append(Technician(name, position))
        self.log(actor.username, "ADD PERSONNEL: " + name)
        self.save()

    def edit_technician(self, actor, index, name, position) -> None:
        self._require_role(actor, "CUSTODIAN")
        tech = self.technicians[index]
        tech.name, tech.position = require_text(name, "Name"), require_text(position, "Position")
        self.log(actor.username, "EDIT PERSONNEL: " + tech.name)
        self.save()

    def delete_technician(self, actor, index) -> None:
        self._require_role(actor, "CUSTODIAN")
        tech = self.technicians.pop(index)
        self.log(actor.username, "DELETE PERSONNEL: " + tech.name)
        self.save()

    def rules_text(self) -> str:
        s = self.settings
        return (
            "1. Submit a borrow request. The custodian will approve or reject it.\n"
            f"2. Students may borrow up to {s['student_days']} day(s), "
            f"professors up to {s['professor_days']} day(s).\n"
            f"3. Max active requests: {s['student_max']} (student), {s['professor_max']} (professor).\n"
            f"4. Late return penalty for students: PHP {s['penalty']} per day.\n"
            "5. Damaged or lost items must be reported when returning."
        )

    # ----- equipment -----
    def reserved(self, equipment_id: str) -> int:
        """Pieces already promised to approved (not yet released) requests."""
        return sum(r.quantity for r in self.requests
                   if r.equipment_id == equipment_id and r.status == "Approved")

    def free_stock(self, item: Equipment) -> int:
        return max(0, item.stock["Available"] - self.reserved(item.equipment_id))

    def add_equipment(self, actor, name, category, total) -> Equipment:
        self._require_role(actor, "CUSTODIAN")
        name, category = require_text(name, "Equipment name"), require_text(category, "Category")
        if total < 1:
            raise BorrowingError("Quantity must be at least 1.")
        item = Equipment.new(self.next_id("equipment", "EQ"), name, category, total)
        self.equipment.append(item)
        self.log(actor.username, f"ADD EQUIPMENT: {item.equipment_id} {name}")
        self.save()
        return item

    def edit_equipment(self, actor, equipment_id, name, category, total) -> None:
        self._require_role(actor, "CUSTODIAN")
        item = self._get_equipment(equipment_id)
        name, category = require_text(name, "Equipment name"), require_text(category, "Category")
        not_available = item.total - item.stock["Available"]
        if total < not_available:
            raise BorrowingError(f"{not_available} piece(s) are borrowed, in maintenance or "
                                 "defective, so the total cannot be lower than that.")
        if total - not_available < self.reserved(equipment_id):
            raise BorrowingError("That total is too low for the approved requests waiting for pickup.")
        item.name, item.category = name, category
        item.stock["Available"] = total - not_available
        self.log(actor.username, "EDIT EQUIPMENT: " + equipment_id)
        self.save()

    def delete_equipment(self, actor, equipment_id) -> None:
        self._require_role(actor, "CUSTODIAN")
        item = self._get_equipment(equipment_id)
        if item.stock["Available"] != item.total:
            raise BorrowingError("Some pieces are borrowed, in maintenance or defective.")
        if any(r.equipment_id == equipment_id and r.status in ("Pending", "Approved")
               for r in self.requests):
            raise BorrowingError("This item has an active request.")
        self.equipment.remove(item)
        self.log(actor.username, "DELETE EQUIPMENT: " + equipment_id)
        self.save()

    # ----- borrowing -----
    def count_active(self, username: str) -> int:
        return sum(1 for r in self.requests
                   if r.username == username and r.status in ACTIVE_STATUSES)

    def is_overdue(self, request: BorrowRequest) -> bool:
        return request.status == "Borrowed" and date.today() > to_date(request.expected_return_date)

    def days_late(self, request: BorrowRequest) -> int:
        return max(0, (date.today() - to_date(request.expected_return_date)).days)

    def total_penalties(self, username: str) -> int:
        return sum(t.penalty for t in self.transactions if t.username == username)

    def submit_request(self, user, equipment_id, quantity, borrow_date, return_date) -> BorrowRequest:
        max_days, max_active = user.borrow_limits(self.settings)
        if self.count_active(user.username) >= max_active:
            raise BorrowingError(f"You already have {max_active} active request(s). "
                                 "Return or cancel one first.")
        item = self._get_equipment(equipment_id)
        if quantity < 1:
            raise BorrowingError("Quantity must be at least 1.")
        if quantity > self.free_stock(item):
            raise BorrowingError(f"Not enough available stock ({self.free_stock(item)} free).")
        today = date.today()
        if borrow_date < today:
            raise BorrowingError("The borrow date cannot be in the past.")
        if return_date < borrow_date:
            raise BorrowingError("The return date cannot be earlier than the borrow date.")
        if (return_date - borrow_date).days > max_days:
            raise BorrowingError(f"You can only borrow for up to {max_days} day(s).")

        request = BorrowRequest(self.next_id("request", "REQ"), user.username, equipment_id,
                                quantity, today.isoformat(), borrow_date.isoformat(),
                                return_date.isoformat())
        self.requests.append(request)
        self.log(user.username, f"REQUEST {request.request_id}: {item.name} x{quantity}")
        self.save()
        return request

    def cancel_request(self, user, request_id) -> None:
        request = self.find_request(request_id)
        if request.username != user.username:
            raise BorrowingError("You can only cancel your own requests.")
        if not request.cancel():
            raise BorrowingError("Only pending or approved requests can be cancelled.")
        self.log(user.username, "CANCEL " + request_id)
        self.save()

    def review_request(self, actor, request_id, approve, remarks="") -> None:
        self._require_role(actor, "CUSTODIAN")
        request = self.find_request(request_id)
        if request.status != "Pending":
            raise BorrowingError("Only pending requests can be reviewed.")
        if approve:
            item = self.find_equipment(request.equipment_id)
            if item is None:
                raise BorrowingError("The item no longer exists.")
            if request.quantity > self.free_stock(item):
                raise BorrowingError("Not enough available stock to approve this request.")
            request.status = "Approved"
            self.log(actor.username, "APPROVE " + request_id)
        else:
            request.remarks = require_text(remarks, "Reason for rejection")
            request.status = "Rejected"
            self.log(actor.username, "REJECT " + request_id)
        self.save()

    def release_item(self, actor, request_id) -> Transaction:
        self._require_role(actor, "CUSTODIAN")
        request = self.find_request(request_id)
        if request.status != "Approved":
            raise BorrowingError("Only approved requests can be released.")
        item = self.find_equipment(request.equipment_id)
        if item is None or not item.check_availability(request.quantity):
            raise BorrowingError("Not enough available stock to release.")
        item.move("Available", "Borrowed", request.quantity)
        request.status = "Borrowed"
        trx = Transaction(self.next_id("transaction", "TRX"), request_id, request.username,
                          item.name, request.quantity, now_text(), actor.username)
        self.transactions.append(trx)
        self.log(actor.username, f"RELEASE {item.name} x{request.quantity} "
                                 f"({request_id}) to {request.username}")
        self.save()
        return trx

    def return_item(self, actor, request_id, damaged_qty) -> Transaction:
        request = self.find_request(request_id)
        if request.status != "Borrowed":
            raise BorrowingError("This request is not currently borrowed.")
        if actor.role != "CUSTODIAN" and actor.username != request.username:
            raise BorrowingError("You can only return your own items.")
        if not 0 <= damaged_qty <= request.quantity:
            raise BorrowingError(f"Damaged/lost pieces must be between 0 and {request.quantity}.")

        item = self._get_equipment(request.equipment_id)
        trx = self.find_transaction(request_id)
        borrower = self.find_user(request.username)
        exempt = borrower.penalty_exempt if borrower else False
        penalty = 0 if exempt else self.days_late(request) * self.settings["penalty"]

        good = request.quantity - damaged_qty
        if good:
            item.move("Borrowed", "Available", good)
        if damaged_qty:
            item.move("Borrowed", "Defective", damaged_qty)
            technician = actor.full_name if actor.role == "CUSTODIAN" else "Unassigned"
            self.maintenance.append(MaintenanceLog(
                self.next_id("log", "MNT"), item.equipment_id, item.name, damaged_qty, "Defective",
                "Reported damaged/lost on return by " + request.username,
                date.today().isoformat(), technician))

        trx.condition = "Good" if damaged_qty == 0 else f"{damaged_qty} damaged/lost"
        trx.date_returned = now_text()
        trx.received_by = actor.username
        trx.penalty = penalty
        request.status = "Returned"

        if actor.username == request.username:
            self.log(actor.username, f"RETURN {item.name} x{request.quantity} ({request_id})")
        else:
            self.log(actor.username, f"RECORD RETURN {item.name} x{request.quantity} "
                                     f"({request_id}) from {request.username}")
        self.save()
        return trx

    # ----- maintenance -----
    def add_maintenance(self, actor, equipment_id, quantity, state, description) -> None:
        self._require_role(actor, "CUSTODIAN")
        item = self._get_equipment(equipment_id)
        if state not in ("Under Maintenance", "Defective"):
            raise BorrowingError("Invalid state.")
        if quantity < 1 or quantity > self.free_stock(item):
            raise BorrowingError(f"Only {self.free_stock(item)} piece(s) can be tagged right now.")
        description = require_text(description, "Description")
        log = MaintenanceLog(self.next_id("log", "MNT"), equipment_id, item.name, quantity, state,
                             description, date.today().isoformat(), actor.full_name)
        self.maintenance.append(log)
        item.move("Available", state, quantity)
        self.log(actor.username, f"MAINTENANCE LOG {log.log_id}: {item.name} x{quantity}")
        self.save()

    def finish_maintenance(self, actor, log_id, repaired) -> None:
        self._require_role(actor, "CUSTODIAN")
        entry = next((m for m in self.maintenance if m.log_id == log_id), None)
        if entry is None or entry.status != "Ongoing":
            raise BorrowingError("That log is not ongoing.")
        item = self.find_equipment(entry.equipment_id)
        if item is not None:
            if repaired:
                item.move(entry.state, "Available", entry.quantity)
            elif entry.state == "Under Maintenance":
                item.move("Under Maintenance", "Defective", entry.quantity)
        entry.status = "Done"
        self.log(actor.username, "FINISH MAINTENANCE " + log_id)
        self.save()

    # ----- admin -----
    def delete_user(self, actor, username) -> None:
        self._require_role(actor, "ADMIN")
        target = self.find_user(username)
        if target is None:
            raise BorrowingError("User not found.")
        if target is actor:
            raise BorrowingError("You cannot delete your own account.")
        if self.count_active(target.username) > 0:
            raise BorrowingError("This user still has active requests.")
        self.users.remove(target)
        self.log(actor.username, "DELETE USER: " + target.username)
        self.save()

    def assign_role(self, actor, username, new_role) -> None:
        self._require_role(actor, "ADMIN")
        target = self.find_user(username)
        if target is None:
            raise BorrowingError("User not found.")
        if target.role == "ADMIN":
            raise BorrowingError("The admin role cannot be changed.")
        if new_role not in REGISTERABLE_ROLES:
            raise BorrowingError("Invalid role.")
        if self.count_active(target.username) > 0:
            raise BorrowingError("This user still has active requests.")
        fields = {k: v for k, v in target.to_dict().items() if k != "role"}
        self.users[self.users.index(target)] = make_user(new_role, **fields)
        self.log(actor.username, f"ASSIGN ROLE {new_role} to {target.username}")
        self.save()

    def reset_password(self, actor, username, new_password) -> None:
        self._require_role(actor, "ADMIN")
        target = self.find_user(username)
        if target is None:
            raise BorrowingError("User not found.")
        if len(new_password) < MIN_PASSWORD_LENGTH:
            raise BorrowingError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
        target.password_hash = hash_password(new_password)
        self.log(actor.username, "RESET PASSWORD: " + target.username)
        self.save()

    def update_settings(self, actor, values: dict) -> None:
        self._require_role(actor, "ADMIN")
        for key, value in values.items():
            if key not in SETTING_MINIMUMS:
                raise BorrowingError("Unknown setting.")
            if value < SETTING_MINIMUMS[key]:
                raise BorrowingError(f"{key.replace('_', ' ').title()} must be at least "
                                     f"{SETTING_MINIMUMS[key]}.")
        self.settings.update(values)
        self.log(actor.username, "CHANGE SETTINGS: " + ", ".join(f"{k}={v}" for k, v in values.items()))
        self.save()

    # ----- reports (return (headings, rows)) -----
    def report_inventory(self):
        rows, sums = [], [0, 0, 0, 0]
        for e in self.equipment:
            s = e.stock
            rows.append((e.name, f"{s['Available']}/{e.total}", s["Borrowed"],
                         s["Under Maintenance"], s["Defective"]))
            for i, state in enumerate(STOCK_STATES):
                sums[i] += s[state]
        rows.append(("TOTAL", sums[0], sums[1], sums[2], sums[3]))
        return ["Equipment", "Available", "Borrowed", "Maintenance", "Defective"], rows

    def report_transactions(self):
        rows = []
        for t in reversed(self.transactions):
            rows.append((t.transaction_id, t.username, t.item_name, t.quantity, t.date_released[:16],
                         t.released_by, (t.date_returned or "Not yet returned")[:16],
                         t.condition or "-", f"PHP {t.penalty}"))
        return (["Transaction", "User", "Item", "Qty", "Released", "Released by",
                 "Returned", "Condition", "Penalty"], rows)

    def report_high_demand(self):
        totals: dict[str, int] = {}
        for t in self.transactions:
            totals[t.item_name] = totals.get(t.item_name, 0) + t.quantity
        ranked = sorted(totals.items(), key=lambda pair: pair[1], reverse=True)
        return ["Rank", "Item", "Pieces borrowed"], [(i + 1, n, q) for i, (n, q) in enumerate(ranked)]

    def report_unreturned_damaged(self):
        rows = []
        for r in self.requests:
            if r.status == "Borrowed":
                note = f"OVERDUE ({self.days_late(r)} day(s))" if self.is_overdue(r) else "On loan"
                rows.append(("Borrowed", self.item_name(r.equipment_id), r.quantity,
                             f"{r.username}, due {r.expected_return_date}", note))
        for e in self.equipment:
            if e.stock["Under Maintenance"]:
                rows.append(("Maintenance", e.name, e.stock["Under Maintenance"], "-", "In repair"))
            if e.stock["Defective"]:
                rows.append(("Defective", e.name, e.stock["Defective"], "-", "Needs attention"))
        return ["Type", "Item", "Qty", "Who / due", "Note"], rows

    def run_report(self, name):
        method = {"Inventory Report": self.report_inventory,
                  "Transaction Report": self.report_transactions,
                  "High-Demand Items": self.report_high_demand,
                  "Unreturned / Damaged Items": self.report_unreturned_damaged}[name]
        return method()

    def system_summary(self) -> dict:
        by_role = {role: 0 for role in USER_CLASSES}
        for u in self.users:
            by_role[u.role] += 1
        borrowed = [r for r in self.requests if r.status == "Borrowed"]
        return {
            "users": len(self.users), "by_role": by_role,
            "equipment_types": len(self.equipment),
            "pieces_total": sum(e.total for e in self.equipment),
            "pieces_available": sum(e.stock["Available"] for e in self.equipment),
            "requests": len(self.requests),
            "pending": sum(1 for r in self.requests if r.status == "Pending"),
            "borrowed": len(borrowed),
            "overdue": sum(1 for r in borrowed if self.is_overdue(r)),
            "maintenance_logs": len(self.maintenance),
            "penalties": sum(t.penalty for t in self.transactions),
            "log_entries": len(self.logs),
        }

    # ----- monitoring -----
    def log_view(self, kind: str, username: str = "") -> list[LogEntry]:
        """Newest first. kind: all, login, mine, staff, professor, student."""
        result = []
        for entry in self.logs:
            role = self.role_of(entry.username)
            is_session = entry.action in ("LOGIN", "LOGOUT")
            keep = {
                "all": True,
                "login": is_session,
                "mine": is_session and entry.username == username,
                "staff": role in ("CUSTODIAN", "ADMIN"),
                "professor": role == "PROFESSOR",
                "student": role == "STUDENT",
            }[kind]
            if keep:
                result.append(entry)
        result.reverse()
        return result
