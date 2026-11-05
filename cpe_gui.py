"""
cpe_gui.py - Tkinter interface for the CpE Equipment Borrowing System.
Run:  python cpe_gui.py      (keep cpe_core.py in the same folder)
"""
import csv
import tkinter as tk
from datetime import date, timedelta
from tkinter import filedialog, messagebox, ttk

from cpe_core import (BorrowingError, LabSystem, parse_date, parse_int, REGISTERABLE_ROLES)

ROLE_LABELS = {
    "Student": "STUDENT",
    "Professor": "PROFESSOR",
    "Custodian / Technical Staff": "CUSTODIAN",
    "Administrator": "ADMIN",
}


# ======================================================================
# Reusable widgets
# ======================================================================
class FormDialog(tk.Toplevel):
    """Modal form. fields = [(key, label, kind[, options])]; kind: text | password | choice."""

    def __init__(self, parent, title, fields, initial=None, ok_text="Save"):
        super().__init__(parent)
        self.title(title)
        self.transient(parent.winfo_toplevel())
        self.resizable(False, False)
        self.result = None
        self._vars, self._kinds = {}, {}
        initial = initial or {}

        body = ttk.Frame(self, padding=16)
        body.pack(fill="both", expand=True)
        first = None
        for row, spec in enumerate(fields):
            key, label, kind = spec[:3]
            options = spec[3] if len(spec) > 3 else None
            ttk.Label(body, text=label).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=5)
            default = options[0] if kind == "choice" else ""
            var = tk.StringVar(value=str(initial.get(key, default)))
            if kind == "choice":
                widget = ttk.Combobox(body, textvariable=var, values=options, state="readonly", width=38)
            else:
                widget = ttk.Entry(body, textvariable=var, width=40, show="•" if kind == "password" else "")
            widget.grid(row=row, column=1, sticky="ew", pady=5)
            self._vars[key], self._kinds[key] = var, kind
            first = first or widget

        buttons = ttk.Frame(body)
        buttons.grid(row=len(fields), column=0, columnspan=2, sticky="e", pady=(14, 0))
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="right", padx=(8, 0))
        ttk.Button(buttons, text=ok_text, style="Accent.TButton", command=self._ok).pack(side="right")
        self.bind("<Return>", lambda e: self._ok())
        self.bind("<Escape>", lambda e: self.destroy())

        first.focus_set()
        self.update_idletasks()
        top = parent.winfo_toplevel()
        x = top.winfo_rootx() + (top.winfo_width() - self.winfo_width()) // 2
        y = top.winfo_rooty() + (top.winfo_height() - self.winfo_height()) // 3
        self.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        self.grab_set()
        self.wait_window(self)

    def _ok(self):
        self.result = {key: (var.get() if self._kinds[key] == "password" else var.get().strip())
                       for key, var in self._vars.items()}
        self.destroy()


def ask_form(parent, title, fields, **kwargs):
    return FormDialog(parent, title, fields, **kwargs).result


class DataTable(ttk.Frame):
    """Treeview with scrollbars. columns = [(heading, width)]; rows = [(iid, values[, tag])]."""

    def __init__(self, parent, columns, height=14):
        super().__init__(parent)
        self.tree = ttk.Treeview(self, show="headings", selectmode="browse", height=height)
        ysb = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        xsb = ttk.Scrollbar(self, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=ysb.set, xscrollcommand=xsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        ysb.grid(row=0, column=1, sticky="ns")
        xsb.grid(row=1, column=0, sticky="ew")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self.tree.tag_configure("overdue", foreground="#b00020")
        self.tree.tag_configure("low", foreground="#b26a00")
        self.tree.tag_configure("done", foreground="#777777")
        self.set_columns(columns)

    def set_columns(self, columns):
        keys = [f"c{i}" for i in range(len(columns))]
        self.tree.configure(columns=keys)
        for key, (heading, width) in zip(keys, columns):
            self.tree.heading(key, text=heading)
            self.tree.column(key, width=width, minwidth=50, anchor="w", stretch=True)

    def set_rows(self, rows):
        keep = self.selected_id()
        self.tree.delete(*self.tree.get_children())
        for row in rows:
            tag = row[2] if len(row) > 2 else ""
            self.tree.insert("", "end", iid=str(row[0]), values=row[1], tags=(tag,) if tag else ())
        if keep is not None and self.tree.exists(keep):
            self.tree.selection_set(keep)

    def selected_id(self):
        selection = self.tree.selection()
        return selection[0] if selection else None


# ======================================================================
# Tabs
# ======================================================================
class Tab(ttk.Frame):
    title = ""

    def __init__(self, main):
        super().__init__(main.notebook, padding=12)
        self.main, self.system, self.user = main, main.system, main.user

    def refresh(self):
        pass

    def run(self, func, *args, ok_message=None):
        """Run a LabSystem action; show rule violations nicely. Returns (ok, result)."""
        try:
            result = func(*args)
        except BorrowingError as error:
            messagebox.showerror("Cannot continue", str(error), parent=self)
            return False, None
        if ok_message:
            messagebox.showinfo("Done", ok_message, parent=self)
        self.main.refresh_all()
        return True, result

    def pick(self, table, what="row"):
        selected = table.selected_id()
        if selected is None:
            messagebox.showinfo("Select a row", f"Please select a {what} in the table first.", parent=self)
        return selected

    def toolbar(self):
        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(0, 8))
        return bar

    @staticmethod
    def add_buttons(bar, buttons):
        """buttons are listed left-to-right; they are packed on the right edge."""
        for text, command in reversed(buttons):
            ttk.Button(bar, text=text, command=command).pack(side="right", padx=3)

    def equipment_choices(self):
        return {f"{e.equipment_id} - {e.name} ({self.system.free_stock(e)} free)": e.equipment_id
                for e in self.system.equipment}


class LabInfoTab(Tab):
    title = "Lab Information"

    def __init__(self, main):
        super().__init__(main)
        self.is_custodian = self.user.role == "CUSTODIAN"

        rules = ttk.LabelFrame(self, text="Borrowing rules", padding=10)
        rules.pack(fill="x")
        self.rules_label = ttk.Label(rules, justify="left")
        self.rules_label.pack(anchor="w")

        people = ttk.LabelFrame(self, text="Laboratory personnel", padding=10)
        people.pack(fill="both", expand=True, pady=10)
        if self.is_custodian:
            bar = ttk.Frame(people)
            bar.pack(fill="x", pady=(0, 6))
            self.add_buttons(bar, [("Add", self.add), ("Edit", self.edit), ("Delete", self.delete)])
        self.table = DataTable(people, [("Name", 260), ("Position", 260)], height=6)
        self.table.pack(fill="both", expand=True)

        profile = ttk.LabelFrame(self, text="My profile", padding=10)
        profile.pack(fill="x")
        u = self.user
        for row, (label, value) in enumerate([
                ("User ID", u.user_id), ("Username", u.username), ("Full name", u.full_name),
                ("Email", u.email), ("Role", u.role), ("ID number", u.id_no)]):
            ttk.Label(profile, text=label + ":", foreground="#666").grid(row=row, column=0, sticky="w", padx=(0, 10))
            ttk.Label(profile, text=value).grid(row=row, column=1, sticky="w")
        ttk.Button(profile, text="Change my password",
                   command=lambda: change_password_dialog(self, self.system, self.user)
                   ).grid(row=0, column=2, rowspan=2, padx=30)

    def refresh(self):
        self.rules_label.configure(text=self.system.rules_text())
        self.table.set_rows([(i, (t.name, t.position)) for i, t in enumerate(self.system.technicians)])

    def add(self):
        v = ask_form(self, "Add personnel", [("name", "Name", "text"), ("position", "Position", "text")])
        if v:
            self.run(self.system.add_technician, self.user, v["name"], v["position"])

    def edit(self):
        index = self.pick(self.table, "person")
        if index is None:
            return
        tech = self.system.technicians[int(index)]
        v = ask_form(self, "Edit personnel", [("name", "Name", "text"), ("position", "Position", "text")],
                     initial={"name": tech.name, "position": tech.position})
        if v:
            self.run(self.system.edit_technician, self.user, int(index), v["name"], v["position"])

    def delete(self):
        index = self.pick(self.table, "person")
        if index is not None and messagebox.askyesno("Delete", "Delete this person?", parent=self):
            self.run(self.system.delete_technician, self.user, int(index))


class EquipmentTab(Tab):
    title = "Equipment"
    COLUMNS = [("ID", 70), ("Equipment", 230), ("Category", 150), ("Available", 80),
               ("Total", 60), ("Borrowed", 80), ("Maintenance", 95), ("Defective", 80)]

    def __init__(self, main):
        super().__init__(main)
        bar = self.toolbar()
        ttk.Label(bar, text="Search:").pack(side="left")
        self.search = tk.StringVar()
        self.search.trace_add("write", lambda *_: self.refresh())
        ttk.Entry(bar, textvariable=self.search, width=30).pack(side="left", padx=6)
        if self.user.role == "CUSTODIAN":
            self.add_buttons(bar, [("Add Equipment", self.add), ("Edit", self.edit), ("Delete", self.delete)])
        self.table = DataTable(self, self.COLUMNS)
        self.table.pack(fill="both", expand=True)

    def refresh(self):
        query = self.search.get().lower().strip()
        rows = []
        for e in self.system.equipment:
            if query and not any(query in text.lower() for text in (e.name, e.category, e.equipment_id)):
                continue
            s = e.stock
            rows.append((e.equipment_id,
                         (e.equipment_id, e.name, e.category, s["Available"], e.total,
                          s["Borrowed"], s["Under Maintenance"], s["Defective"]),
                         "low" if s["Available"] == 0 else ""))
        self.table.set_rows(rows)

    FIELDS = [("name", "Equipment name", "text"), ("category", "Category", "text"),
              ("total", "Total quantity", "text")]

    def add(self):
        v = ask_form(self, "Add equipment", self.FIELDS, initial={"total": "1"}, ok_text="Add")
        if v:
            self.run(lambda: self.system.add_equipment(
                self.user, v["name"], v["category"], parse_int(v["total"], "Quantity")))

    def edit(self):
        eid = self.pick(self.table, "equipment item")
        if eid is None:
            return
        item = self.system.find_equipment(eid)
        v = ask_form(self, "Edit equipment", self.FIELDS,
                     initial={"name": item.name, "category": item.category, "total": item.total})
        if v:
            self.run(lambda: self.system.edit_equipment(
                self.user, eid, v["name"], v["category"], parse_int(v["total"], "Quantity")))

    def delete(self):
        eid = self.pick(self.table, "equipment item")
        if eid is not None and messagebox.askyesno("Delete", "Delete this equipment?", parent=self):
            self.run(self.system.delete_equipment, self.user, eid)


class BorrowingTab(Tab):
    title = "Borrowing"
    COLUMNS = [("Request", 80), ("User", 110), ("Item", 190), ("Qty", 45), ("Borrow date", 95),
               ("Return by", 95), ("Status", 90), ("Details", 280)]
    FILTERS = ["All", "Pending", "Approved", "Borrowed", "Overdue", "Returned", "Rejected", "Cancelled"]

    def __init__(self, main):
        super().__init__(main)
        role = self.user.role
        bar = self.toolbar()
        ttk.Label(bar, text="Show:").pack(side="left")
        self.filter = tk.StringVar(value="All")
        combo = ttk.Combobox(bar, textvariable=self.filter, values=self.FILTERS, state="readonly", width=12)
        combo.pack(side="left", padx=6)
        combo.bind("<<ComboboxSelected>>", lambda e: self.refresh())

        self.scope = tk.StringVar(value="mine")
        if role == "PROFESSOR":
            for text, value in (("My requests", "mine"), ("All requests", "all")):
                ttk.Radiobutton(bar, text=text, value=value, variable=self.scope,
                                command=self.refresh).pack(side="left", padx=(10, 0))

        if role == "CUSTODIAN":
            self.add_buttons(bar, [("Approve", self.approve), ("Reject", self.reject),
                                   ("Release Item", self.release), ("Record Return", self.record_return)])
        elif role in ("STUDENT", "PROFESSOR"):
            self.add_buttons(bar, [("New Request", self.new_request), ("Cancel Request", self.cancel),
                                   ("Return Equipment", self.return_own)])

        self.table = DataTable(self, self.COLUMNS)
        self.table.pack(fill="both", expand=True)
        self.footer = ttk.Label(self, foreground="#666")
        self.footer.pack(anchor="w", pady=(8, 0))

    # --- table ---
    def visible_requests(self):
        requests = list(self.system.requests)
        if self.user.role == "STUDENT" or (self.user.role == "PROFESSOR" and self.scope.get() == "mine"):
            requests = [r for r in requests if r.username == self.user.username]
        choice = self.filter.get()
        if choice == "Overdue":
            requests = [r for r in requests if self.system.is_overdue(r)]
        elif choice != "All":
            requests = [r for r in requests if r.status == choice]
        return list(reversed(requests))

    def details(self, r):
        if self.system.is_overdue(r):
            return f"OVERDUE by {self.system.days_late(r)} day(s)"
        trx = self.system.find_transaction(r.request_id)
        if r.status == "Returned" and trx:
            return f"Returned {trx.date_returned[:16]} | {trx.condition} | Penalty PHP {trx.penalty}"
        return r.remarks

    def refresh(self):
        rows = []
        for r in self.visible_requests():
            tag = "overdue" if self.system.is_overdue(r) else (
                "done" if r.status in ("Returned", "Rejected", "Cancelled") else "")
            rows.append((r.request_id,
                         (r.request_id, r.username, self.system.item_name(r.equipment_id), r.quantity,
                          r.borrow_date, r.expected_return_date, r.status, self.details(r)), tag))
        self.table.set_rows(rows)
        if self.user.role in ("STUDENT", "PROFESSOR"):
            max_days, max_active = self.user.borrow_limits(self.system.settings)
            text = (f"Active requests: {self.system.count_active(self.user.username)}/{max_active}  |  "
                    f"Max loan: {max_days} day(s)")
            if self.user.role == "STUDENT":
                text += f"  |  My total penalties: PHP {self.system.total_penalties(self.user.username)}"
            self.footer.configure(text=text)

    # --- borrower actions ---
    def new_request(self):
        max_days, _ = self.user.borrow_limits(self.system.settings)
        choices = self.equipment_choices()
        if not choices:
            messagebox.showinfo("No equipment", "There is no equipment to borrow yet.", parent=self)
            return
        today = date.today()
        v = ask_form(self, "New borrow request", [
            ("item", "Equipment", "choice", list(choices)),
            ("qty", "Quantity", "text"),
            ("borrow", "Borrow date (YYYY-MM-DD)", "text"),
            ("ret", f"Return date (max {max_days} day(s))", "text")],
            initial={"qty": "1", "borrow": today.isoformat(),
                     "ret": (today + timedelta(days=max_days)).isoformat()},
            ok_text="Submit")
        if not v:
            return
        self.run(lambda: self.system.submit_request(
            self.user, choices[v["item"]], parse_int(v["qty"], "Quantity"),
            parse_date(v["borrow"], "Borrow date"), parse_date(v["ret"], "Return date")),
            ok_message="Request submitted. It is now waiting for approval.")

    def cancel(self):
        rid = self.pick(self.table, "request")
        if rid is not None:
            self.run(self.system.cancel_request, self.user, rid, ok_message="Request cancelled.")

    def _return(self, rid):
        request = self.system.find_request(rid)
        v = ask_form(self, "Return equipment",
                     [("dmg", f"Damaged / lost pieces (0 = all good, max {request.quantity})", "text")],
                     initial={"dmg": "0"}, ok_text="Confirm return")
        if not v:
            return
        ok, trx = self.run(lambda: self.system.return_item(
            self.user, rid, parse_int(v["dmg"], "Damaged pieces", 0)))
        if ok:
            message = "Return recorded."
            if trx.penalty:
                message += f"\nLate return penalty: PHP {trx.penalty}"
            messagebox.showinfo("Return successful", message, parent=self)

    def return_own(self):
        rid = self.pick(self.table, "borrowed item")
        if rid is not None:
            if self.system.find_request(rid).status != "Borrowed":
                messagebox.showinfo("Not borrowed", "Only items you are currently borrowing can be returned.",
                                    parent=self)
                return
            self._return(rid)

    # --- custodian actions ---
    def approve(self):
        rid = self.pick(self.table, "request")
        if rid is not None:
            self.run(self.system.review_request, self.user, rid, True, ok_message="Request approved.")

    def reject(self):
        rid = self.pick(self.table, "request")
        if rid is None:
            return
        v = ask_form(self, "Reject request", [("reason", "Reason for rejection", "text")], ok_text="Reject")
        if v:
            self.run(self.system.review_request, self.user, rid, False, v["reason"], ok_message="Request rejected.")

    def release(self):
        rid = self.pick(self.table, "approved request")
        if rid is not None:
            ok, trx = self.run(self.system.release_item, self.user, rid)
            if ok:
                messagebox.showinfo("Released", f"Item released. Transaction: {trx.transaction_id}", parent=self)

    def record_return(self):
        rid = self.pick(self.table, "borrowed item")
        if rid is not None:
            if self.system.find_request(rid).status != "Borrowed":
                messagebox.showinfo("Not borrowed", "Select a request with status 'Borrowed'.", parent=self)
                return
            self._return(rid)


class MaintenanceTab(Tab):
    title = "Maintenance"
    COLUMNS = [("Log", 70), ("Item", 190), ("Qty", 45), ("State", 120), ("Date", 90),
               ("Status", 80), ("Technician", 140), ("Description", 280)]

    def __init__(self, main):
        super().__init__(main)
        if self.user.role == "CUSTODIAN":
            bar = self.toolbar()
            self.add_buttons(bar, [("Add Maintenance / Defect Log", self.add), ("Finish Selected", self.finish)])
        self.table = DataTable(self, self.COLUMNS)
        self.table.pack(fill="both", expand=True)

    def refresh(self):
        self.table.set_rows([
            (m.log_id, (m.log_id, m.item_name, m.quantity, m.state, m.date_recorded,
                        m.status, m.technician, m.description), "done" if m.status == "Done" else "")
            for m in reversed(self.system.maintenance)])

    def add(self):
        choices = self.equipment_choices()
        if not choices:
            return
        v = ask_form(self, "Add maintenance / defect log", [
            ("item", "Equipment", "choice", list(choices)),
            ("qty", "How many pieces", "text"),
            ("state", "Tag the pieces as", "choice", ["Under Maintenance", "Defective"]),
            ("desc", "Description", "text")], initial={"qty": "1"}, ok_text="Record")
        if v:
            self.run(lambda: self.system.add_maintenance(
                self.user, choices[v["item"]], parse_int(v["qty"], "Quantity"), v["state"], v["desc"]),
                ok_message="Log recorded.")

    def finish(self):
        log_id = self.pick(self.table, "log")
        if log_id is None:
            return
        repaired_text = "Repaired - pieces become Available"
        v = ask_form(self, "Finish maintenance", [
            ("outcome", "Outcome", "choice", [repaired_text, "Cannot be repaired - pieces stay Defective"])],
            ok_text="Finish")
        if v:
            self.run(self.system.finish_maintenance, self.user, log_id, v["outcome"] == repaired_text)


class ReportsTab(Tab):
    title = "Reports"

    def __init__(self, main):
        super().__init__(main)
        bar = self.toolbar()
        ttk.Label(bar, text="Report:").pack(side="left")
        self.report = tk.StringVar(value=LabSystem.REPORTS[0])
        combo = ttk.Combobox(bar, textvariable=self.report, values=LabSystem.REPORTS,
                             state="readonly", width=28)
        combo.pack(side="left", padx=6)
        combo.bind("<<ComboboxSelected>>", lambda e: self.refresh())
        self.add_buttons(bar, [("Export to CSV", self.export)])
        self.table = DataTable(self, [("", 100)])
        self.table.pack(fill="both", expand=True)
        self.headings, self.rows = [], []

    def refresh(self):
        self.headings, self.rows = self.system.run_report(self.report.get())
        self.table.set_columns([(h, 150) for h in self.headings])
        self.table.set_rows([(i, row) for i, row in enumerate(self.rows)])

    def export(self):
        path = filedialog.asksaveasfilename(
            parent=self, defaultextension=".csv", filetypes=[("CSV file", "*.csv")],
            initialfile=self.report.get().lower().replace(" / ", "_").replace(" ", "_") + ".csv")
        if path:
            with open(path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(self.headings)
                writer.writerows(self.rows)
            messagebox.showinfo("Exported", f"Saved to:\n{path}", parent=self)


class MonitoringTab(Tab):
    title = "Monitoring"

    def __init__(self, main):
        super().__init__(main)
        role = self.user.role
        if role in ("CUSTODIAN", "ADMIN"):
            self.views = {"All system logs": "all", "Login / logout history": "login",
                          "Custodian / admin activity": "staff", "Professor activity": "professor",
                          "Student activity": "student"}
        elif role == "PROFESSOR":
            self.views = {"My login / logout history": "mine", "Student activity": "student"}
        else:
            self.views = {"My login / logout history": "mine"}

        bar = self.toolbar()
        ttk.Label(bar, text="View:").pack(side="left")
        self.view = tk.StringVar(value=next(iter(self.views)))
        combo = ttk.Combobox(bar, textvariable=self.view, values=list(self.views), state="readonly", width=30)
        combo.pack(side="left", padx=6)
        combo.bind("<<ComboboxSelected>>", lambda e: self.refresh())
        self.table = DataTable(self, [("Time", 150), ("User", 130), ("Action", 600)])
        self.table.pack(fill="both", expand=True)

    def refresh(self):
        entries = self.system.log_view(self.views[self.view.get()], self.user.username)[:1000]
        self.table.set_rows([(i, (e.timestamp, e.username, e.action)) for i, e in enumerate(entries)])


class UsersTab(Tab):
    title = "User Management"
    COLUMNS = [("User ID", 80), ("Username", 130), ("Full name", 200), ("Role", 100),
               ("Email", 210), ("ID number", 120)]

    def __init__(self, main):
        super().__init__(main)
        summary = ttk.LabelFrame(self, text="System summary", padding=10)
        summary.pack(fill="x", pady=(0, 8))
        self.summary_label = ttk.Label(summary, justify="left")
        self.summary_label.pack(anchor="w")

        bar = self.toolbar()
        self.add_buttons(bar, [("Assign Role", self.assign_role), ("Reset Password", self.reset_password),
                               ("Delete User", self.delete)])
        self.table = DataTable(self, self.COLUMNS)
        self.table.pack(fill="both", expand=True)

    def refresh(self):
        s = self.system.system_summary()
        r = s["by_role"]
        self.summary_label.configure(text=(
            f"Users: {s['users']}  (Students {r['STUDENT']}, Professors {r['PROFESSOR']}, "
            f"Custodians {r['CUSTODIAN']}, Admins {r['ADMIN']})\n"
            f"Equipment types: {s['equipment_types']}  |  Pieces available: "
            f"{s['pieces_available']}/{s['pieces_total']}\n"
            f"Requests: {s['requests']} (Pending {s['pending']})  |  Borrowed now: {s['borrowed']}  |  "
            f"Overdue: {s['overdue']}\n"
            f"Maintenance logs: {s['maintenance_logs']}  |  Total penalties: PHP {s['penalties']}  |  "
            f"Log entries: {s['log_entries']}"))
        self.table.set_rows([(u.username, (u.user_id, u.username, u.full_name, u.role, u.email, u.id_no))
                             for u in self.system.users])

    def assign_role(self):
        username = self.pick(self.table, "user")
        if username is None:
            return
        v = ask_form(self, "Assign role", [("role", "New role", "choice", list(REGISTERABLE_ROLES))],
                     ok_text="Assign")
        if v:
            self.run(self.system.assign_role, self.user, username, v["role"], ok_message="Role updated.")

    def reset_password(self):
        username = self.pick(self.table, "user")
        if username is None:
            return
        v = ask_form(self, f"Reset password for {username}", [("pw", "New password", "password")],
                     ok_text="Reset")
        if v:
            self.run(self.system.reset_password, self.user, username, v["pw"], ok_message="Password reset.")

    def delete(self):
        username = self.pick(self.table, "user")
        if username is not None and messagebox.askyesno("Delete user", f"Delete '{username}'?", parent=self):
            self.run(self.system.delete_user, self.user, username)


class SettingsTab(Tab):
    title = "Department Settings"
    FIELDS = [("student_days", "Student max borrow days"),
              ("professor_days", "Professor max borrow days"),
              ("student_max", "Student max active requests"),
              ("professor_max", "Professor max active requests"),
              ("penalty", "Late penalty per day (PHP, students only)")]

    def __init__(self, main):
        super().__init__(main)
        box = ttk.LabelFrame(self, text="Borrowing rules", padding=16)
        box.pack(anchor="nw")
        self.vars = {}
        for row, (key, label) in enumerate(self.FIELDS):
            ttk.Label(box, text=label).grid(row=row, column=0, sticky="w", padx=(0, 20), pady=6)
            var = tk.StringVar()
            ttk.Spinbox(box, from_=0, to=1000, textvariable=var, width=8).grid(row=row, column=1, pady=6)
            self.vars[key] = var
        ttk.Button(box, text="Save settings", style="Accent.TButton",
                   command=self.save).grid(row=len(self.FIELDS), column=0, columnspan=2, sticky="e", pady=(12, 0))

    def refresh(self):
        for key, var in self.vars.items():
            var.set(str(self.system.settings[key]))

    def save(self):
        def do():
            values = {key: parse_int(var.get(), label)
                      for (key, label), var in zip(self.FIELDS, self.vars.values())}
            self.system.update_settings(self.user, values)
        self.run(do, ok_message="Settings updated.")


# ======================================================================
# Screens
# ======================================================================
def change_password_dialog(parent, system, user):
    v = ask_form(parent, "Change password", [
        ("old", "Current password", "password"),
        ("new", "New password", "password"),
        ("confirm", "Confirm new password", "password")], ok_text="Change")
    if not v:
        return
    if v["new"] != v["confirm"]:
        messagebox.showerror("Cannot continue", "The new passwords do not match.", parent=parent)
        return
    try:
        system.change_password(user, v["old"], v["new"])
    except BorrowingError as error:
        messagebox.showerror("Cannot continue", str(error), parent=parent)
        return
    messagebox.showinfo("Done", "Password changed.", parent=parent)


class LoginFrame(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        card = ttk.Frame(self, padding=30)
        card.place(relx=0.5, rely=0.45, anchor="center")

        ttk.Label(card, text="Engineering Equipment Borrowing System", style="Title.TLabel").grid(
            row=0, column=0, columnspan=2, pady=(0, 2))
        ttk.Label(card, text="Computer Engineering (CpE) Laboratory", foreground="#666").grid(
            row=1, column=0, columnspan=2, pady=(0, 22))

        self.role = tk.StringVar(value="Student")
        self.username, self.password = tk.StringVar(), tk.StringVar()
        ttk.Label(card, text="I am a").grid(row=2, column=0, sticky="w", pady=6)
        ttk.Combobox(card, textvariable=self.role, values=list(ROLE_LABELS), state="readonly",
                     width=30).grid(row=2, column=1, pady=6, padx=(10, 0))
        ttk.Label(card, text="Username").grid(row=3, column=0, sticky="w", pady=6)
        self.username_entry = ttk.Entry(card, textvariable=self.username, width=33)
        self.username_entry.grid(row=3, column=1, pady=6, padx=(10, 0))
        ttk.Label(card, text="Password").grid(row=4, column=0, sticky="w", pady=6)
        ttk.Entry(card, textvariable=self.password, show="•", width=33).grid(row=4, column=1, pady=6, padx=(10, 0))

        buttons = ttk.Frame(card)
        buttons.grid(row=5, column=0, columnspan=2, pady=(18, 0), sticky="ew")
        ttk.Button(buttons, text="Login", style="Accent.TButton", command=self.do_login).pack(side="left", expand=True, fill="x", padx=(0, 6))
        ttk.Button(buttons, text="Register", command=self.do_register).pack(side="left", expand=True, fill="x")

        self.username_entry.focus_set()
        app.bind("<Return>", lambda e: self.do_login())

    def selected_role(self):
        return ROLE_LABELS[self.role.get()]

    def do_login(self):
        try:
            user = self.app.system.login(self.username.get(), self.password.get(), self.selected_role())
        except BorrowingError as error:
            messagebox.showerror("Login failed", str(error), parent=self)
            self.password.set("")
            return
        self.app.unbind("<Return>")
        self.app.on_login(user)

    def do_register(self):
        role = self.selected_role()
        if role == "ADMIN":
            messagebox.showinfo("Register", "Admin accounts cannot be registered.", parent=self)
            return
        fields = []
        if role != "STUDENT":
            fields.append(("code", f"{role.title()} access code", "password"))
        fields += [("username", "Username", "text"), ("password", "Password", "password"),
                   ("confirm", "Confirm password", "password"), ("name", "Full name", "text"),
                   ("email", "Email", "text"),
                   ("id_no", "Student number" if role == "STUDENT" else "Employee number", "text")]
        v = ask_form(self, f"Register ({self.role.get()})", fields, ok_text="Register")
        if not v:
            return
        if v["password"] != v["confirm"]:
            messagebox.showerror("Register", "The passwords do not match.", parent=self)
            return
        try:
            user = self.app.system.register(role, v.get("code", ""), v["username"], v["password"],
                                            v["name"], v["email"], v["id_no"])
        except BorrowingError as error:
            messagebox.showerror("Register", str(error), parent=self)
            return
        messagebox.showinfo("Registered", f"Registered successfully!\nYour User ID is {user.user_id}.\n"
                                          "You can log in now.", parent=self)
        self.username.set(user.username)
        self.password.set("")


class MainFrame(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app, self.system, self.user = app, app.system, app.user

        header = ttk.Frame(self, padding=(14, 10))
        header.pack(fill="x")
        ttk.Label(header, text="CpE Equipment Borrowing System", style="Header.TLabel").pack(side="left")
        ttk.Button(header, text="Logout", command=app.logout).pack(side="right")
        ttk.Label(header, text=f"{self.user.full_name}  ({self.user.role})").pack(side="right", padx=14)

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        role = self.user.role
        classes = [LabInfoTab, EquipmentTab, BorrowingTab]
        if role in ("CUSTODIAN", "ADMIN"):
            classes += [MaintenanceTab, ReportsTab]
        classes.append(MonitoringTab)
        if role == "ADMIN":
            classes += [UsersTab, SettingsTab]

        self.tabs = []
        for cls in classes:
            tab = cls(self)
            self.notebook.add(tab, text=cls.title)
            self.tabs.append(tab)
        self.notebook.bind("<<NotebookTabChanged>>", lambda e: self.tabs[self.notebook.index("current")].refresh())
        self.refresh_all()

    def refresh_all(self):
        for tab in self.tabs:
            tab.refresh()


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("CpE Equipment Borrowing System")
        self.geometry("1120x700")
        self.minsize(960, 600)
        self.system = LabSystem()
        self.user = None
        self._setup_style()
        self.container = ttk.Frame(self)
        self.container.pack(fill="both", expand=True)
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.show_login()

    def _setup_style(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Treeview", rowheight=26)
        style.configure("Treeview.Heading", font=("TkDefaultFont", 10, "bold"))
        style.configure("Title.TLabel", font=("TkDefaultFont", 18, "bold"))
        style.configure("Header.TLabel", font=("TkDefaultFont", 14, "bold"))
        style.configure("Accent.TButton", font=("TkDefaultFont", 10, "bold"))
        style.configure("TNotebook.Tab", padding=(14, 6))

    def _clear(self):
        for widget in self.container.winfo_children():
            widget.destroy()

    def show_login(self):
        self._clear()
        LoginFrame(self.container, self).pack(fill="both", expand=True)

    def on_login(self, user):
        self.user = user
        self._clear()
        MainFrame(self.container, self).pack(fill="both", expand=True)
        if self.system.uses_default_password(user):
            messagebox.showwarning("Change your password",
                                   "This account still uses the default password.\n"
                                   "Please choose a new one now.", parent=self)
            change_password_dialog(self, self.system, user)

    def logout(self):
        if self.user is not None:
            self.system.logout(self.user)
        self.user = None
        self.show_login()

    def on_close(self):
        self.system.save()
        self.destroy()

    def report_callback_exception(self, exc, val, tb):
        messagebox.showerror("Unexpected error", f"{exc.__name__}: {val}")


if __name__ == "__main__":
    App().mainloop()

