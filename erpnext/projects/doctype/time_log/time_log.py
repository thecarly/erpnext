# Copyright (c) 2015, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

from __future__ import unicode_literals
import frappe, json
from frappe import _
from frappe.utils import cstr, flt, get_datetime, get_time, getdate
from dateutil.relativedelta import relativedelta

class OverlapError(frappe.ValidationError): pass
class OverProductionLoggedError(frappe.ValidationError): pass
class NotSubmittedError(frappe.ValidationError): pass
class NegativeHoursError(frappe.ValidationError): pass

from frappe.model.document import Document

class TimeLog(Document):
	def validate(self):
		self.set_status()
		self.set_title()
		self.validate_overlap()
		self.validate_timings()
		self.calculate_total_hours()
		self.validate_time_log_for()
		self.check_workstation_timings()
		self.validate_production_order()
		self.validate_manufacturing()
		self.validate_task()
		self.update_cost()

	def on_submit(self):
		self.update_production_order()
		self.update_task()

	def on_cancel(self):
		self.update_production_order()
		self.update_task()

	def before_update_after_submit(self):
		self.set_status()

	def before_cancel(self):
		self.set_status()

	def set_status(self):
		self.status = {
			0: "Draft",
			1: "Submitted",
			2: "Cancelled"
		}[self.docstatus or 0]

		if self.time_log_batch:
			self.status="Batched for Billing"

		if self.sales_invoice:
			self.status="Billed"

	def set_title(self):
		if self.production_order:
			self.title = _("{0} for {1}").format(self.operation, self.production_order)
		elif self.task:
			self.title = _("{0} for {1}").format(self.activity_type, self.task)
		elif self.project:
			self.title = _("{0} for {1}").format(self.activity_type, self.project)
		else:
			self.title = self.activity_type

	def validate_overlap(self):
		"""Checks if 'Time Log' entries overlap for a user, workstation. """
		self.validate_overlap_for("user")
		self.validate_overlap_for("employee")
		self.validate_overlap_for("workstation")

	def validate_overlap_for(self, fieldname):
		existing = self.get_overlap_for(fieldname)
		if existing:
			frappe.throw(_("This Time Log conflicts with {0} for {1} {2}").format(existing.name,
				self.meta.get_label(fieldname), self.get(fieldname)), OverlapError)

	def get_overlap_for(self, fieldname):
		if not self.get(fieldname):
			return

		existing = frappe.db.sql("""select name, from_time, to_time from `tabTime Log`
			where `{0}`=%(val)s and
			(
				(from_time > %(from_time)s and from_time < %(to_time)s) or
				(to_time > %(from_time)s and to_time < %(to_time)s) or
				(%(from_time)s > from_time and %(from_time)s < to_time) or
				(%(from_time)s = from_time and %(to_time)s = to_time))
			and name!=%(name)s
			and ifnull(task, "")=%(task)s
			and docstatus < 2""".format(fieldname),
			{
				"val": self.get(fieldname),
				"from_time": self.from_time,
				"to_time": self.to_time,
				"name": self.name or "No Name",
				"task": cstr(self.task)
			}, as_dict=True)

		return existing[0] if existing else None

	def validate_timings(self):
		if self.to_time and self.from_time and get_datetime(self.to_time) <= get_datetime(self.from_time):
			frappe.throw(_("To Time must be greater than From Time"), NegativeHoursError)

	def calculate_total_hours(self):
		if self.to_time and self.from_time:
			from frappe.utils import time_diff_in_seconds
			self.hours = flt(time_diff_in_seconds(self.to_time, self.from_time)) / 3600

	def validate_time_log_for(self):
		if not self.for_manufacturing:
			for fld in ["production_order", "operation", "workstation", "completed_qty"]:
				self.set(fld, None)
		else:
			self.activity_type=None

	def check_workstation_timings(self):
		"""Checks if **Time Log** is between operating hours of the **Workstation**."""
		if self.workstation and self.from_time and self.to_time:
			from erpnext.manufacturing.doctype.workstation.workstation import check_if_within_operating_hours
			check_if_within_operating_hours(self.workstation, self.operation, self.from_time, self.to_time)

	def validate_production_order(self):
		"""Throws 'NotSubmittedError' if **production order** is not submitted. """
		if self.production_order:
			if frappe.db.get_value("Production Order", self.production_order, "docstatus") != 1 :
				frappe.throw(_("You can make a time log only against a submitted production order"), NotSubmittedError)

	def update_production_order(self):
		"""Updates `start_date`, `end_date`, `status` for operation in Production Order."""

		if self.production_order and self.for_manufacturing:
			if not self.operation_id:
				frappe.throw(_("Operation ID not set"))

			dates = self.get_operation_start_end_time()
			summary = self.get_time_log_summary()

			pro = frappe.get_doc("Production Order", self.production_order)
			for o in pro.operations:
				if o.name == self.operation_id:
					o.actual_start_time = dates.start_date
					o.actual_end_time = dates.end_date
					o.completed_qty = summary.completed_qty
					o.actual_operation_time = summary.mins
					break


			pro.flags.ignore_validate_update_after_submit = True
			pro.update_operation_status()
			pro.calculate_operating_cost()
			pro.set_actual_dates()
			pro.save()

	def get_operation_start_end_time(self):
		"""Returns Min From and Max To Dates of Time Logs against a specific Operation. """
		return frappe.db.sql("""select min(from_time) as start_date, max(to_time) as end_date from `tabTime Log`
				where production_order = %s and operation = %s and docstatus=1""",
				(self.production_order, self.operation), as_dict=1)[0]

	def move_to_next_day(self):
		"""Move start and end time one day forward"""
		self.from_time = get_datetime(self.from_time) + relativedelta(day=1)

	def move_to_next_working_slot(self):
		"""Move to next working slot from workstation"""
		workstation = frappe.get_doc("Workstation", self.workstation)
		slot_found = False
		for working_hour in workstation.working_hours:
			if get_datetime(self.from_time).time() < get_time(working_hour.start_time):
				self.from_time = getdate(self.from_time).strftime("%Y-%m-%d") + " " + working_hour.start_time
				slot_found = True
				break

		if not slot_found:
			# later than last time
			self.from_time = getdate(self.from_time).strftime("%Y-%m-%d") + " " + workstation.working_hours[0].start_time
			self.move_to_next_day()

	def move_to_next_non_overlapping_slot(self):
		"""If in overlap, set start as the end point of the overlapping time log"""
		overlapping = self.get_overlap_for("workstation")
		if overlapping:
			self.from_time = get_datetime(overlapping.to_time) + relativedelta(minutes=10)

	def get_time_log_summary(self):
		"""Returns 'Actual Operating Time'. """
		return frappe.db.sql("""select
			sum(hours*60) as mins, sum(ifnull(completed_qty, 0)) as completed_qty
			from `tabTime Log`
			where production_order = %s and operation_id = %s and docstatus=1""",
			(self.production_order, self.operation_id), as_dict=1)[0]

	def validate_manufacturing(self):
		if self.for_manufacturing:
			if not self.production_order:
				frappe.throw(_("Production Order is Mandatory"))
			if not self.completed_qty:
				self.completed_qty = 0

			production_order = frappe.get_doc("Production Order", self.production_order)
			pending_qty = flt(production_order.qty) - flt(production_order.produced_qty)
			if flt(self.completed_qty) > pending_qty:
				frappe.throw(_("Completed Qty cannot be more than {0} for operation {1}").format(pending_qty, self.operation),
					OverProductionLoggedError)

		else:
			self.production_order = None
			self.operation = None
			self.quantity = None

	def update_cost(self):
		rate = get_activity_cost(self.employee, self.activity_type)
		if rate:
			self.costing_rate = rate.get('costing_rate')
			self.billing_rate = rate.get('billing_rate')
			self.costing_amount = self.costing_rate * self.hours
			if self.billable:
				self.billing_amount = self.billing_rate * self.hours
			else:
				self.billing_amount = 0

	def validate_task(self):
		# if a time log is being created against a project without production order
		if (self.project and not self.production_order) and not self.task:
			frappe.throw(_("Task is Mandatory if Time Log is against a project"))

	def update_task(self):
		if self.task and frappe.db.exists("Task", self.task):
			task = frappe.get_doc("Task", self.task)
			task.update_time_and_costing()
			task.save()

@frappe.whitelist()
def get_events(start, end, filters=None):
	"""Returns events for Gantt / Calendar view rendering.

	:param start: Start date-time.
	:param end: End date-time.
	:param filters: Filters like workstation, project etc.
	"""
	from frappe.desk.reportview import build_match_conditions
	if not frappe.has_permission("Time Log"):
		frappe.msgprint(_("No Permission"), raise_exception=1)

	conditions = build_match_conditions("Time Log")
	conditions = conditions and (" and " + conditions) or ""
	if filters:
		filters = json.loads(filters)
		for key in filters:
			if filters[key]:
				conditions += " and " + key + ' = "' + filters[key].replace('"', '\"') + '"'

	data = frappe.db.sql("""select name, from_time, to_time,
		activity_type, task, project, production_order, workstation from `tabTime Log`
		where docstatus < 2 and ( from_time between %(start)s and %(end)s or to_time between %(start)s and %(end)s )
		{conditions}""".format(conditions=conditions), {
			"start": start,
			"end": end
			}, as_dict=True, update={"allDay": 0})

	for d in data:
		d.title = d.name + ": " + (d.activity_type or d.production_order or "")
		if d.task:
			d.title += " for Task: " + d.task
		if d.project:
			d.title += " for Project: " + d.project

	return data

@frappe.whitelist()
def get_activity_cost(employee=None, activity_type=None):
	rate = frappe.db.sql("""select costing_rate, billing_rate from `tabActivity Cost` where employee= %s
		and activity_type= %s""", (employee, activity_type), as_dict=1)
	if not rate:
		rate = frappe.db.sql("""select costing_rate, billing_rate from `tabActivity Cost` where ifnull(employee, '')=''
			and activity_type= %s""", (activity_type), as_dict=1)
	return rate[0] if rate else {}
