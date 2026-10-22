# This file is part of Tryton.  The COPYRIGHT file at the top level of
# this repository contains the full copyright notices and license terms.
from calendar import monthrange
from datetime import date, datetime, timedelta
from math import ceil

from dateutil.relativedelta import relativedelta
from sql import Null
from sql.aggregate import Max, Sum
from sql.conditionals import Coalesce
from trytond.bus import notify
from trytond.i18n import gettext
from trytond.model import (
    ModelSQL, ModelSingleton, ModelView, Workflow, Unique, fields)
from trytond.pool import Pool
from trytond.pyson import Eval, PYSONEncoder
from trytond.transaction import Transaction
from trytond.exceptions import UserError
from trytond.wizard import Button, StateTransition, StateView, Wizard

from .product import FORECAST_METHODS

DAY_TYPES = [
    ('festive', 'Festivo'),
    ('product_day', 'Diada Productos'),
    ('special_sale', 'Venta Especial'),
]
WEEKDAYS = [
    ('0', 'Monday'),
    ('1', 'Tuesday'),
    ('2', 'Wednesday'),
    ('3', 'Thursday'),
    ('4', 'Friday'),
    ('5', 'Saturday'),
    ('6', 'Sunday'),
]
SPECIAL_SCOPES = [
    ('all_products', 'Todos los Productos'),
    ('product_category', 'Categoría de Producto'),
    ('party_category', 'Categoría de Cliente'),
]
FORECAST_METHOD_LABELS = dict(FORECAST_METHODS)


def _context_company():
    company_id = Transaction().context.get('company')
    if company_id:
        return Pool().get('company.company')(company_id)


def _packaging_capabilities(product_template):
    package = (getattr(product_template, 'sales_forecast_package', None)
        or getattr(product_template, 'default_package', None))
    if package and not package.active:
        raise UserError(gettext(
            'sales_forecast.msg_inactive_forecast_package',
            product=product_template.rec_name))
    package_quantity = getattr(package, 'quantity', None) if package else None
    box_per_pallet = getattr(package, 'box_per_pallet', None) if package else None
    box_per_pallet = (box_per_pallet
        or getattr(product_template, 'sales_forecast_boxes_per_pallet', None))
    return package, package_quantity, box_per_pallet


def _convert_packaging_quantities(product_template, units=None, boxes=None,
        pallets=None, round_up=False):
    package, package_quantity, box_per_pallet = _packaging_capabilities(
        product_template)

    if units is None and (boxes is not None or pallets is not None):
        if not package_quantity or (pallets is not None and not box_per_pallet):
            raise UserError(gettext(
                'sales_forecast.msg_missing_forecast_packaging',
                product=product_template.rec_name))
    if package_quantity:
        if units is None and boxes is not None:
            units = boxes * package_quantity
        if units is None and pallets is not None and box_per_pallet:
            boxes = pallets * box_per_pallet
            units = boxes * package_quantity
        if boxes is None and units is not None:
            boxes = units / package_quantity

    if box_per_pallet:
        if pallets is None and boxes is not None:
            pallets = boxes / box_per_pallet
        if boxes is None and pallets is not None:
            boxes = pallets * box_per_pallet
            if units is None and package_quantity:
                units = boxes * package_quantity

    if units is None:
        units = 0.0
    if round_up:
        boxes = ceil(boxes) if boxes is not None else None
        pallets = ceil(pallets) if pallets is not None else None
    return {
        'units': round(units, 2),
        'boxes': round(boxes, 2) if boxes is not None else None,
        'pallets': round(pallets, 2) if pallets is not None else None,
    }


def previous_year_date(value, years=1):
    year, week, weekday = value.isocalendar()
    year -= years
    last_week = date(year, 12, 28).isocalendar().week
    return date.fromisocalendar(year, min(week, last_week), weekday)


def _insert_rows(cursor, table, columns, values, chunk_size=1000):
    for index in range(0, len(values), chunk_size):
        chunk = values[index:index + chunk_size]
        cursor.execute(*table.insert(columns=columns, values=chunk))


class SalesForecastSaleHistory(ModelSQL, ModelView):
    'Sales Forecast Sale History'
    __name__ = 'sales.forecast.sale_history'

    company = fields.Many2One('company.company', 'Company', required=True,
        ondelete='CASCADE')
    product_template = fields.Many2One('product.template', 'Product',
        required=True, ondelete='CASCADE',
        context={
            'company': Eval('company', -1),
        })
    date = fields.Date('Date', required=True)
    quantity = fields.Float('Quantity', required=True, readonly=True)
    computed_at = fields.DateTime('Computed At', required=True, readonly=True)

    @classmethod
    def __setup__(cls):
        super().__setup__()
        table = cls.__table__()
        cls._sql_constraints = [
            ('sale_history_company_product_date_unique',
                Unique(table, table.company, table.product_template,
                    table.date),
                'sales_forecast.msg_sale_history_company_product_date_unique'),
        ]
        cls._order.insert(0, ('date', 'DESC'))
        cls._order.insert(1, ('product_template', 'ASC'))

    @classmethod
    def __register__(cls, module_name):
        table_h = cls.__table_handler__(module_name)
        company_exists = table_h.column_exist('company')
        table_h.drop_constraint('sale_history_product_date_unique')
        super().__register__(module_name)
        if not company_exists:
            cursor = Transaction().connection.cursor()
            cursor.execute(*cls.__table__().delete())
            table_h.not_null_action('company', action='add')

    @classmethod
    def last_date(cls, company):
        cursor = Transaction().connection.cursor()
        table = cls.__table__()

        cursor.execute(*table.select(
                Max(table.date),
                where=table.company == company.id))
        row = cursor.fetchone()
        return row[0] if row else None

    @classmethod
    def update_history(cls, company=None):
        cursor = Transaction().connection.cursor()
        table = cls.__table__()
        today = Pool().get('ir.date').today()
        if company is None:
            company_id = Transaction().context.get('company')
            if company_id:
                company = Pool().get('company.company')(company_id)
        if company is None:
            raise UserError(gettext(
                'sales_forecast.msg_missing_history_company'))

        # A sale may be confirmed, cancelled or re-dated before the last cached
        # date. Refresh the whole company history, including removed quantities.
        cls.lock()
        rows = cls._aggregated_sale_rows(
            company, end_date=today)
        cursor.execute(*table.delete(where=table.company == company.id))
        if not rows:
            return

        computed_at = datetime.now()
        values = [[company.id, template_id, sale_date,
                float(quantity or 0.0), computed_at]
            for template_id, sale_date, quantity in rows]
        _insert_rows(cursor, table, [
                table.company,
                table.product_template,
                table.date,
                table.quantity,
                table.computed_at,
                ], values)

    @classmethod
    def _aggregated_sale_rows(cls, company, start_date=None, end_date=None,
            party_category=None):
        pool = Pool()
        Sale = pool.get('sale.sale')
        SaleLine = pool.get('sale.line')
        Product = pool.get('product.product')
        Template = pool.get('product.template')
        Uom = pool.get('product.uom')
        cursor = Transaction().connection.cursor()

        sale = Sale.__table__()
        sale_line = SaleLine.__table__()
        product = Product.__table__()
        template = Template.__table__()
        sale_day = Coalesce(sale.shipping_date, sale.sale_date)

        query = sale_line.join(
            sale, condition=sale_line.sale == sale.id).join(
            product, condition=sale_line.product == product.id).join(
            template, condition=product.template == template.id)
        where = (
            (sale_line.type == 'line')
            & (sale_line.product != Null)
            & sale.state.in_(['confirmed', 'processing', 'done'])
            & (sale.company == company.id)
            & (sale_day != Null)
            & (template.salable == True)
            & (template.type == 'goods')
        )
        if party_category:
            Category = pool.get('party.category')
            Relation = pool.get('party.party-party.category').__table__()
            categories = Category.search([
                ('parent', 'child_of', [party_category.id])])
            where &= sale.party.in_(Relation.select(Relation.party,
                where=Relation.category.in_([c.id for c in categories])))
        if start_date:
            where &= sale_day >= start_date
        if end_date:
            where &= sale_day <= end_date

        cursor.execute(*query.select(
                product.template,
                sale_day,
                sale_line.unit,
                template.default_uom,
                Sum(sale_line.quantity),
                where=where,
                group_by=[
                    product.template, sale_day,
                    sale_line.unit, template.default_uom,
                    ],
                order_by=[product.template, sale_day],
                ))
        quantities = {}
        incompatible_units = set()
        for template_id, sale_date, unit_id, default_uom_id, quantity \
                in cursor.fetchall():
            if isinstance(sale_date, str):
                sale_date = date.fromisoformat(sale_date)
            unit = Uom(unit_id)
            default_uom = Uom(default_uom_id)
            if unit.category != default_uom.category:
                incompatible_units.add((template_id, unit_id, default_uom_id))
                continue
            converted = Uom.compute_qty(
                unit, float(quantity or 0.0), default_uom, round=False)
            key = (template_id, sale_date)
            quantities.setdefault(key, 0.0)
            quantities[key] += converted
        if incompatible_units:
            details = []
            for template_id, unit_id, default_uom_id in sorted(
                    incompatible_units):
                cursor.execute(*query.select(
                        sale.id, sale.number,
                        where=(where
                            & (product.template == template_id)
                            & (sale_line.unit == unit_id)),
                        distinct=True, order_by=[sale.id]))
                sales = ', '.join(number or str(sale_id)
                    for sale_id, number in cursor.fetchall())
                details.append('%s (%s → %s): %s' % (
                    Template(template_id).rec_name,
                    Uom(unit_id).rec_name, Uom(default_uom_id).rec_name,
                    sales))
            notify(gettext('sales_forecast.msg_history_incompatible_units'),
                body='\n'.join(details), priority=2)
        return [
            (template_id, sale_date, quantity)
            for (template_id, sale_date), quantity in quantities.items()
            ]

    @classmethod
    def quantity_sum(cls, company, template, start_date, end_date):
        cursor = Transaction().connection.cursor()
        table = cls.__table__()
        where = (
            (table.company == company.id)
            & (table.product_template == template.id)
            & (table.date >= start_date)
            & (table.date <= end_date)
        )
        cursor.execute(*table.select(Sum(table.quantity), where=where))
        row = cursor.fetchone()
        return float((row[0] if row else 0.0) or 0.0)

    @classmethod
    def distribution_quantities(cls, company, template, start_date, end_date,
            excluded_dates=None):
        cursor = Transaction().connection.cursor()
        table = cls.__table__()
        quantities = {str(i): 0.0 for i in range(7)}

        where = (
            (table.company == company.id)
            & (table.product_template == template.id)
            & (table.date >= start_date)
            & (table.date <= end_date)
        )
        if excluded_dates:
            where &= ~table.date.in_(list(excluded_dates))
        cursor.execute(*table.select(
                table.date, table.quantity,
                where=where))
        for sale_date, quantity in cursor.fetchall():
            quantities[str(sale_date.weekday())] += float(quantity or 0.0)
        return quantities

    @classmethod
    def template_totals(cls, company, start_date, end_date, template_ids):
        cursor = Transaction().connection.cursor()
        table = cls.__table__()
        totals = {}
        if not template_ids:
            return totals

        cursor.execute(*table.select(
                table.product_template,
                Sum(table.quantity),
                where=(
                    (table.company == company.id)
                    & table.product_template.in_(template_ids)
                    & (table.date >= start_date)
                    & (table.date <= end_date)
                ),
                group_by=[table.product_template],
                ))
        for template_id, quantity in cursor.fetchall():
            totals[template_id] = float(quantity or 0.0)
        return totals

    @classmethod
    def quantity_map(cls, company, template_ids, dates):
        cursor = Transaction().connection.cursor()
        table = cls.__table__()
        quantities = {}
        if not template_ids or not dates:
            return quantities

        cursor.execute(*table.select(
                table.product_template,
                table.date,
                table.quantity,
                where=(
                    (table.company == company.id)
                    & table.product_template.in_(template_ids)
                    & table.date.in_(dates)
                )))
        for template_id, sale_date, quantity in cursor.fetchall():
            quantities[(template_id, sale_date)] = float(quantity or 0.0)
        return quantities

    @classmethod
    def range_quantity_map(cls, company, template_ids, start_date, end_date):
        cursor = Transaction().connection.cursor()
        table = cls.__table__()
        quantities = {}
        if not template_ids or not start_date or not end_date:
            return quantities

        cursor.execute(*table.select(
                table.product_template,
                table.date,
                table.quantity,
                where=(
                    (table.company == company.id)
                    & table.product_template.in_(template_ids)
                    & (table.date >= start_date)
                    & (table.date <= end_date)
                )))
        for template_id, sale_date, quantity in cursor.fetchall():
            quantities[(template_id, sale_date)] = float(quantity or 0.0)
        return quantities


class SalesForecastSaleHistoryUpdateStart(ModelView):
    'Sales Forecast Sale History Update'
    __name__ = 'sales.forecast.sale_history.update.start'

    last_date = fields.Date('Current Last Date', readonly=True)

    @staticmethod
    def default_last_date():
        company = _context_company()
        if company:
            return Pool().get('sales.forecast.sale_history').last_date(company)


class SalesForecastSaleHistoryUpdate(Wizard):
    'Sales Forecast Sale History Update'
    __name__ = 'sales.forecast.sale_history.update'
    start = StateView(
        'sales.forecast.sale_history.update.start',
        'sales_forecast.sale_history_update_start_view_form', [
            Button('Cancel', 'end', 'tryton-cancel'),
            Button('Update', 'update', 'tryton-ok', default=True),
        ])
    update = StateTransition()

    def transition_update(self):
        Pool().get('sales.forecast.sale_history').update_history(
            company=_context_company())
        return 'end'


class SalesForecastCalendarCalculateDistributions(Wizard):
    'Sales Forecast Calendar Calculate Distributions'
    __name__ = 'sales.forecast.calendar.calculate_distributions'

    start = StateView(
        'sales.forecast.calendar.calculate_distributions.start',
        'sales_forecast.calendar_calculate_distributions_start_view_form', [
            Button('Cancel', 'end', 'tryton-cancel'),
            Button('Calculate', 'calculate', 'tryton-ok', default=True),
        ])
    start_state = 'start'
    calculate = StateTransition()

    def transition_calculate(self):
        Calendar = Pool().get('sales.forecast.calendar')
        calendars = Calendar.active_calendars()
        if calendars:
            Calendar.calculate_distributions(calendars)
            notify(gettext('sales_forecast.msg_distributions_calculated'))
        return 'end'


class SalesForecastCalendarCalculateDistributionsStart(ModelView):
    'Sales Forecast Calendar Calculate Distributions'
    __name__ = 'sales.forecast.calendar.calculate_distributions.start'


class SalesForecastCalendarCalculateEventResultsStart(ModelView):
    'Sales Forecast Calendar Calculate Event Results'
    __name__ = 'sales.forecast.calendar.calculate_event_results.start'


class SalesForecastCalendarCalculateEventResults(Wizard):
    'Sales Forecast Calendar Calculate Event Results'
    __name__ = 'sales.forecast.calendar.calculate_event_results'

    start = StateView(
        'sales.forecast.calendar.calculate_event_results.start',
        'sales_forecast.calendar_calculate_event_results_start_view_form', [
            Button('Cancel', 'end', 'tryton-cancel'),
            Button('Calculate', 'calculate', 'tryton-ok', default=True),
        ])
    start_state = 'start'
    calculate = StateTransition()

    def transition_calculate(self):
        Calendar = Pool().get('sales.forecast.calendar')
        calendars = Calendar.active_calendars()
        if calendars:
            Calendar.calculate_event_results(calendars)
            notify(gettext('sales_forecast.msg_event_results_calculated'))
        return 'end'


class SalesForecastCalendar(ModelSQL, ModelView):
    'Sales Forecast Calendar'
    __name__ = 'sales.forecast.calendar'

    name = fields.Char('Name', required=True)
    active = fields.Boolean('Active')
    day_lines = fields.One2Many('sales.forecast.calendar.day',
        'calendar', 'Days')
    distribution_calculated_at = fields.DateTime(
        'Distribution Calculated At', readonly=True)
    events_calculated_at = fields.DateTime(
        'Events Calculated At', readonly=True)

    @staticmethod
    def default_active():
        return True

    @classmethod
    def __register__(cls, module_name):
        super().__register__(module_name)
        if not cls.search([], limit=1):
            cls.create([{
                        'name': 'Calendario Base Previsiones',
                        'active': True,
                        }])

    @classmethod
    def active_calendars(cls):
        calendars = cls.search([
            ('active', '=', True),
        ])
        if calendars:
            return calendars
        return cls.search([])

    @classmethod
    def __setup__(cls):
        super().__setup__()
        cls._buttons.update({
            'calculate_distributions': {
                'icon': 'tryton-refresh',
            },
            'calculate_event_results': {
                'icon': 'tryton-refresh',
            },
        })

    @classmethod
    @ModelView.button
    def calculate_distributions(cls, calendars):
        pool = Pool()
        Company = pool.get('company.company')
        Template = pool.get('product.template')
        Distribution = pool.get('sales.forecast.distribution')
        company_id = Transaction().context.get('company')
        if not company_id:
            raise UserError(gettext(
                'sales_forecast.msg_missing_history_company'))
        company = Company(company_id)

        if not Transaction().context.get('sales_forecast_history_ready'):
            pool.get('sales.forecast.sale_history').update_history(
                company=company)

        templates = Template.search([
            ('salable', '=', True),
            ('type', '=', 'goods'),
            ('active', '=', True),
        ], order=[('name', 'ASC')])
        existing = Distribution.search([
                ('company', '=', company.id),
                ])
        if existing:
            Distribution.delete(existing)

        now = datetime.now()
        to_create = []
        excluded_by_product = {}
        for calendar in calendars:
            for day in calendar.day_lines:
                affected = set()
                if day.day_type == 'product_day':
                    affected = {line.product_template.id
                        for line in day.product_lines}
                elif day.day_type == 'special_sale':
                    for line in day.special_lines:
                        if line.scope == 'party_category':
                            rows = pool.get('sales.forecast.sale_history'
                                )._aggregated_sale_rows(company,
                                    start_date=day.date, end_date=day.date,
                                    party_category=line.party_category)
                            affected.update(row[0] for row in rows)
                        elif line.scope == 'product_category':
                            affected.update(t.id for t in Template.search([
                                ('type', '=', 'goods'), ('salable', '=', True),
                                ('categories_all', 'child_of',
                                    [line.product_category.id], 'parent')]))
                        else:
                            affected.update(t.id for t in templates)
                for template_id in affected:
                    excluded_by_product.setdefault(template_id, set()).add(
                        day.date)
        for template in templates:
            percentages = cls._compute_distribution_percentages(
                company, template,
                excluded_dates=excluded_by_product.get(template.id))
            percentages = {day: round(value, 4)
                for day, value in percentages.items()}
            percentages['6'] = round(100.0 - sum(
                    percentages[str(day)] for day in range(6)), 4)
            for weekday, percentage in percentages.items():
                to_create.append({
                    'company': company.id,
                    'product_template': template.id,
                    'weekday': weekday,
                    'percentage': round(percentage, 4),
                    'computed_at': now,
                })
        if to_create:
            Distribution.create(to_create)
        cls.write(calendars, {
            'distribution_calculated_at': now,
        })

    @classmethod
    def _compute_distribution_percentages(cls, company, template,
            excluded_dates=None):
        quantities = cls._distribution_quantities(
            company, template, excluded_dates=excluded_dates)
        total = sum(quantities.values())
        if total <= 0:
            # No usable history: persist the neutral distribution explicitly.
            return {str(i): 100.0 / 7.0 for i in range(7)}
        return {
            weekday: quantities[weekday] * 100.0 / total
            for weekday in quantities
        }

    @classmethod
    def _distribution_quantities(cls, company, template,
            excluded_dates=None):
        SaleHistory = Pool().get('sales.forecast.sale_history')
        today = Pool().get('ir.date').today()
        start_date = today - relativedelta(years=1) + timedelta(days=1)
        return SaleHistory.distribution_quantities(
            company, template, start_date, today,
            excluded_dates=excluded_dates)

    @classmethod
    @ModelView.button
    def calculate_event_results(cls, calendars):
        pool = Pool()
        Company = pool.get('company.company')
        Result = pool.get('sales.forecast.event_result')
        company_id = Transaction().context.get('company')
        if not company_id:
            raise UserError(gettext(
                'sales_forecast.msg_missing_history_company'))
        company = Company(company_id)

        if not Transaction().context.get('sales_forecast_history_ready'):
            pool.get('sales.forecast.sale_history').update_history(
                company=company)

        now = datetime.now()
        for calendar in calendars:
            existing = Result.search([
                ('calendar_day.calendar', '=', calendar.id),
                ('company', '=', company.id),
            ])
            if existing:
                Result.delete(existing)
            to_create = []
            for day in calendar.day_lines:
                if day.day_type == 'product_day':
                    to_create.extend(cls._product_day_results(company, day))
                elif day.day_type == 'special_sale':
                    to_create.extend(cls._special_sale_results(company, day))
            if to_create:
                Result.create(to_create)
        cls.write(calendars, {
            'events_calculated_at': now,
        })

    @classmethod
    def _product_day_results(cls, company, calendar_day):
        results = []
        for line in calendar_day.product_lines:
            if line.product_template.type != 'goods':
                continue
            package, _, _ = _packaging_capabilities(line.product_template)
            quantities = _convert_packaging_quantities(
                line.product_template,
                units=line.units,
                boxes=line.boxes,
                pallets=line.pallets, round_up=True)
            results.append({
                'company': company.id,
                'calendar_day': calendar_day.id,
                'product_template': line.product_template.id,
                'origin': 'product_day',
                'package': package.id if package else None,
                'units': quantities['units'],
                'boxes': quantities['boxes'],
                'pallets': quantities['pallets'],
            })
        return results

    @classmethod
    def _special_sale_results(cls, company, calendar_day):
        Pool().get('sales.forecast.calendar.day.special').validate(
            list(calendar_day.special_lines))
        aggregated = {}
        for line in calendar_day.special_lines:
            baseline = cls._special_sale_baseline(
                company, calendar_day, line)
            for template, quantity in baseline.items():
                increased = max(
                    0.0, quantity * (1 + (line.increase_percentage or 0.0)))
                package, _, _ = _packaging_capabilities(template)
                quantities = _convert_packaging_quantities(
                    template, units=round(increased, 2))
                values = aggregated.setdefault(template.id, {
                        'company': company.id,
                        'calendar_day': calendar_day.id,
                        'product_template': template.id,
                        'origin': 'special_sale',
                        'package': package.id if package else None,
                        'units': 0.0,
                        'boxes': 0.0,
                        'pallets': 0.0,
                        })
                values['units'] += quantities['units']
                for name in ('boxes', 'pallets'):
                    if values[name] is None or quantities[name] is None:
                        values[name] = None
                    else:
                        values[name] += quantities[name]
        return [{
                **values,
                'units': round(values['units'], 2),
                'boxes': (ceil(values['boxes'])
                    if values['boxes'] is not None else None),
                'pallets': (ceil(values['pallets'])
                    if values['pallets'] is not None else None),
                } for values in aggregated.values()]

    @classmethod
    def _special_sale_baseline(cls, company, calendar_day, special_line):
        pool = Pool()
        SaleHistory = pool.get('sales.forecast.sale_history')
        Template = pool.get('product.template')

        reference = previous_year_date(calendar_day.date)
        monday = reference - timedelta(days=reference.weekday())
        friday = monday + timedelta(days=4)

        if special_line.scope == 'party_category' and special_line.party_category:
            return cls._special_sale_party_category_baseline(
                company, monday, friday, special_line)

        template_domain = [
            ('salable', '=', True),
            ('type', '=', 'goods'),
            ('active', '=', True),
        ]
        if special_line.scope == 'product_category' and special_line.product_category:
            template_domain.append(
                ('categories_all', 'child_of',
                    [special_line.product_category.id], 'parent'))
        templates = Template.search(template_domain)
        totals_by_id = SaleHistory.template_totals(
            company, monday, friday,
            [template.id for template in templates])
        totals = {template: totals_by_id.get(template.id, 0.0)
            for template in templates}
        return {
            template: quantity / 5.0
            for template, quantity in totals.items()
        }

    @classmethod
    def _special_sale_party_category_baseline(cls, company, monday, friday,
            special_line):
        pool = Pool()
        History = pool.get('sales.forecast.sale_history')
        Template = pool.get('product.template')
        totals = {}
        for template_id, _, quantity in History._aggregated_sale_rows(
                company, start_date=monday, end_date=friday,
                party_category=special_line.party_category):
            template = Template(template_id)
            if template.active:
                totals.setdefault(template, 0.0)
                totals[template] += quantity
        return {template: quantity / 5.0
            for template, quantity in totals.items()}


class SalesForecastCalendarDay(ModelSQL, ModelView):
    'Sales Forecast Calendar Day'
    __name__ = 'sales.forecast.calendar.day'

    calendar = fields.Many2One('sales.forecast.calendar',
        'Calendar', required=True, ondelete='CASCADE')
    date = fields.Date('Date', required=True)
    day_type = fields.Selection(DAY_TYPES, 'Day Type', required=True)
    note = fields.Char('Name')
    product_lines = fields.One2Many(
        'sales.forecast.calendar.day.product', 'calendar_day',
        'Productos Diada',
        states={
            'invisible': Eval('day_type') != 'product_day',
            })
    special_lines = fields.One2Many(
        'sales.forecast.calendar.day.special', 'calendar_day',
        'Alcances Venta Especial',
        help='Add one line for each scope. If there are several product or '
        'customer categories, create one line per category.',
        states={
            'invisible': Eval('day_type') != 'special_sale',
            })
    result_lines = fields.One2Many(
        'sales.forecast.event_result', 'calendar_day',
        'Resultados del Cálculo', readonly=True)

    @staticmethod
    def default_calendar():
        Calendar = Pool().get('sales.forecast.calendar')
        calendar_id = Transaction().context.get('calendar')
        if calendar_id:
            return calendar_id
        calendars = Calendar.active_calendars()
        if calendars:
            return calendars[0].id

    @classmethod
    def __setup__(cls):
        super().__setup__()
        table = cls.__table__()
        cls._sql_constraints = [
            ('calendar_date_unique',
                Unique(table, table.calendar, table.date),
                'sales_forecast.msg_calendar_date_unique'),
        ]
        cls._order.insert(0, ('date', 'ASC'))
        cls._buttons.update({
            'calculate_special_sale': {
                'icon': 'tryton-refresh',
                'invisible': Eval('day_type') != 'special_sale',
            },
        })

    @classmethod
    @ModelView.button
    def calculate_special_sale(cls, days):
        pool = Pool()
        Calendar = pool.get('sales.forecast.calendar')
        Result = pool.get('sales.forecast.event_result')
        company_id = Transaction().context.get('company')
        if not company_id:
            raise UserError(gettext(
                'sales_forecast.msg_missing_history_company'))
        days = [day for day in days if day.day_type == 'special_sale']
        if not days:
            return
        company = pool.get('company.company')(company_id)
        pool.get('sales.forecast.sale_history').update_history(company=company)
        existing = Result.search([
            ('calendar_day', 'in', [day.id for day in days]),
            ('company', '=', company.id),
        ])
        Result.delete(existing)
        values = []
        for day in days:
            values.extend(Calendar._special_sale_results(company, day))
        if values:
            Result.create(values)

    @classmethod
    def validate(cls, records):
        super().validate(records)
        for record in records:
            if record.day_type == 'product_day' and not record.product_lines:
                raise UserError(gettext(
                    'sales_forecast.msg_missing_product_day_lines',
                    date=record.date))
            if record.day_type == 'special_sale' and not record.special_lines:
                raise UserError(gettext(
                    'sales_forecast.msg_missing_special_sale_lines',
                    date=record.date))

    @classmethod
    def create(cls, vlist):
        records = super().create(vlist)
        Pool().get('sales.forecast.calendar').write(
            list({record.calendar for record in records}), {
                'events_calculated_at': None,
                'distribution_calculated_at': None,
                })
        return records

    @classmethod
    def delete(cls, records):
        calendars = list({record.calendar for record in records})
        super().delete(records)
        if calendars:
            Pool().get('sales.forecast.calendar').write(calendars, {
                    'events_calculated_at': None,
                    'distribution_calculated_at': None,
                    })

    @classmethod
    def write(cls, *args):
        actions = iter(args)
        updated = []
        for records, values in zip(actions, actions):
            for record in records:
                changes = values.copy()
                note = changes.get('note', record.note) or ''
                if Transaction().check_access and note.startswith('[AUTO]'):
                    changes['note'] = note.removeprefix('[AUTO]').lstrip(' |')
                updated.extend(([record], changes))
        super().write(*updated)
        calendars = list({record.calendar for records in args[::2]
                for record in records})
        if calendars:
            Pool().get('sales.forecast.calendar').write(calendars, {
                    'events_calculated_at': None,
                    'distribution_calculated_at': None,
                    })


class CalendarEventLineMixin:
    "Invalidate cached parameters and preserve user edits to automatic days."
    __slots__ = ()

    @classmethod
    def create(cls, vlist):
        records = super().create(vlist)
        if Transaction().check_access:
            Pool().get('sales.forecast.calendar.day').write(
                list({r.calendar_day for r in records}), {})
        return records

    @classmethod
    def write(cls, *args):
        super().write(*args)
        if Transaction().check_access:
            Pool().get('sales.forecast.calendar.day').write(
                list({r.calendar_day for records in args[::2]
                        for r in records}), {})

    @classmethod
    def delete(cls, records):
        days = list({r.calendar_day for r in records})
        super().delete(records)
        if Transaction().check_access:
            Pool().get('sales.forecast.calendar.day').write(days, {})


class SalesForecastCalendarDayProduct(
        CalendarEventLineMixin, ModelSQL, ModelView):
    'Sales Forecast Calendar Day Product'
    __name__ = 'sales.forecast.calendar.day.product'

    calendar_day = fields.Many2One('sales.forecast.calendar.day',
        'Calendar Day', required=True, ondelete='CASCADE')
    product_template = fields.Many2One('product.template', 'Product',
        required=True, ondelete='RESTRICT',
        domain=[('salable', '=', True), ('type', '=', 'goods')])
    units = fields.Float('Venta en unidades')
    boxes = fields.Float('Venta en cajas')
    pallets = fields.Float('Venta en pallets')

    @classmethod
    def __setup__(cls):
        super().__setup__()
        table = cls.__table__()
        cls._sql_constraints += [
            ('day_product_unique',
                Unique(table, table.calendar_day, table.product_template),
                'sales_forecast.msg_duplicate_product_day'),
            ]

    @classmethod
    def _normalize_vlist(cls, vlist):
        pool = Pool()
        Template = pool.get('product.template')
        normalized = []
        for values in vlist:
            values = values.copy()
            template_id = values.get('product_template')
            if template_id:
                template = Template(template_id)
                if all(values.get(name) is None
                        for name in ('units', 'boxes', 'pallets')):
                    raise UserError(gettext(
                        'sales_forecast.msg_missing_product_day_quantity',
                        product=template.rec_name))
                quantities = _convert_packaging_quantities(
                    template,
                    units=values.get('units'),
                    boxes=values.get('boxes'),
                    pallets=values.get('pallets'))
                values.update(quantities)
            normalized.append(values)
        return normalized

    @classmethod
    def create(cls, vlist):
        return super().create(cls._normalize_vlist(vlist))

    @classmethod
    def write(cls, *args):
        actions = iter(args)
        new_args = []
        for records, values in zip(actions, actions):
            quantity_fields = [
                field_name for field_name in ('units', 'boxes', 'pallets')
                if field_name in values and values[field_name] is not None]
            if quantity_fields:
                values = values.copy()
                if len(records) != 1:
                    raise UserError(gettext(
                        'sales_forecast.msg_product_day_single_update'))
                changed_fields = [
                    field_name for field_name in quantity_fields
                    if values[field_name] != getattr(records[0], field_name)]
                source = (changed_fields[0]
                    if len(changed_fields) == 1 else quantity_fields[0])
                values.update(_convert_packaging_quantities(
                        records[0].product_template,
                        **{source: values[source]}))
            new_args.extend([records, values])
        super().write(*new_args)

    @classmethod
    def validate(cls, records):
        super().validate(records)
        for record in records:
            if not any(value is not None for value in [
                        record.units, record.boxes, record.pallets]):
                raise UserError(gettext(
                    'sales_forecast.msg_missing_product_day_quantity',
                    product=record.product_template.rec_name))


class SalesForecastCalendarDaySpecial(
        CalendarEventLineMixin, ModelSQL, ModelView):
    'Sales Forecast Calendar Day Special'
    __name__ = 'sales.forecast.calendar.day.special'

    calendar_day = fields.Many2One('sales.forecast.calendar.day',
        'Calendar Day', required=True, ondelete='CASCADE')
    scope = fields.Selection(SPECIAL_SCOPES, 'Alcance', required=True,
        help='Si hay varias categorías, cree una línea por categoría.')
    product_category = fields.Many2One('product.category',
        'Categoría de Producto',
        states={
            'invisible': Eval('scope') != 'product_category',
            'required': Eval('scope') == 'product_category',
            })
    party_category = fields.Many2One('party.category',
        'Categoría de Cliente',
        states={
            'invisible': Eval('scope') != 'party_category',
            'required': Eval('scope') == 'party_category',
            })
    increase_percentage = fields.Float(
        'Porcentaje aumento venta',
        required=True,
        help='Introduzca un porcentaje: 20 para 20%%; 350 para 350%%.')

    @staticmethod
    def default_scope():
        return 'all_products'

    @classmethod
    def validate(cls, records):
        super().validate(records)
        pool = Pool()
        for day in {record.calendar_day for record in records}:
            lines = cls.search([('calendar_day', '=', day.id)])
            scopes = {line.scope for line in lines}
            if len(scopes) > 1 or (len(lines) > 1
                    and 'all_products' in scopes):
                raise UserError(gettext(
                    'sales_forecast.msg_special_scope_overlap', date=day.date))
            seen_categories = set()
            seen_records = set()
            for line in lines:
                if line.scope == 'all_products':
                    continue
                if line.scope == 'product_category':
                    category = line.product_category
                    Model = pool.get('product.template')
                    domain = [('categories_all', 'child_of',
                            [category.id], 'parent')]
                    Category = pool.get('product.category')
                    category_ids = {c.id for c in Category.search([
                            ('parent', 'child_of', [category.id]),
                            ])} | {category.id}
                else:
                    category = line.party_category
                    Model = pool.get('party.party')
                    domain = [('categories', '=', category.id)]
                    category_ids = {category.id}
                with Transaction().set_context(active_test=False):
                    record_ids = {record.id for record in Model.search(domain)}
                if (seen_categories.intersection(category_ids)
                        or seen_records.intersection(record_ids)):
                    raise UserError(gettext(
                        'sales_forecast.msg_special_scope_overlap',
                        date=day.date))
                seen_categories.update(category_ids)
                seen_records.update(record_ids)


class SalesForecastDistribution(ModelSQL, ModelView):
    'Sales Forecast Distribution'
    __name__ = 'sales.forecast.distribution'

    company = fields.Many2One('company.company', 'Company', required=True,
        ondelete='CASCADE')
    product_template = fields.Many2One('product.template', 'Product',
        required=True, ondelete='CASCADE',
        context={
            'company': Eval('company', -1),
        })
    sequence = fields.Integer('Sequence', readonly=True)
    weekday = fields.Selection(WEEKDAYS, 'Weekday', required=True)
    percentage = fields.Float('Percentage', required=True)
    computed_at = fields.DateTime('Computed At', required=True, readonly=True)

    @classmethod
    def __setup__(cls):
        super().__setup__()
        table = cls.__table__()
        cls._sql_constraints = [
            ('distribution_product_weekday_unique',
                Unique(table, table.company, table.product_template,
                    table.weekday),
                'sales_forecast.msg_distribution_product_weekday_unique'),
        ]
        cls._order.insert(0, ('sequence', 'ASC'))
        cls._order.insert(1, ('weekday', 'ASC'))

    @classmethod
    def __register__(cls, module_name):
        table_h = cls.__table_handler__(module_name)
        company_exists = table_h.column_exist('company')
        table_h.drop_constraint('distribution_product_weekday_unique')
        super().__register__(module_name)
        if not company_exists:
            cursor = Transaction().connection.cursor()
            cursor.execute(*cls.__table__().delete())
            table_h.not_null_action('company', action='add')

    @staticmethod
    def _weekday_sequence(weekday):
        if weekday is None:
            return None
        return int(weekday)

    @classmethod
    def create(cls, vlist):
        vlist = [values.copy() for values in vlist]
        for values in vlist:
            if 'weekday' in values and 'sequence' not in values:
                values['sequence'] = cls._weekday_sequence(values['weekday'])
        return super().create(vlist)

    @classmethod
    def write(cls, *args):
        actions = iter(args)
        updated_args = []
        for records, values in zip(actions, actions):
            values = values.copy()
            if 'weekday' in values and 'sequence' not in values:
                values['sequence'] = cls._weekday_sequence(values['weekday'])
            updated_args.extend((records, values))
        super().write(*updated_args)


class SalesForecastEventResult(ModelSQL, ModelView):
    'Sales Forecast Event Result'
    __name__ = 'sales.forecast.event_result'

    company = fields.Many2One('company.company', 'Company', required=True,
        ondelete='CASCADE')
    calendar_day = fields.Many2One('sales.forecast.calendar.day',
        'Día calendario', required=True, ondelete='CASCADE')
    date = fields.Function(fields.Date('Date'), 'get_date',
        searcher='search_date')
    product_template = fields.Many2One('product.template', 'Product',
        required=True, ondelete='RESTRICT',
        context={
            'company': Eval('company', -1),
        })
    origin = fields.Selection([
        ('product_day', 'Diada Productos'),
        ('special_sale', 'Venta Especial'),
    ], 'Origin', required=True, readonly=True)
    package = fields.Many2One('product.package', 'Forecast Package',
        readonly=True, ondelete='SET NULL')
    units = fields.Float('Venta en unidades', readonly=True)
    boxes = fields.Float('Venta en cajas', readonly=True)
    pallets = fields.Float('Venta en pallets', readonly=True)

    def get_date(self, name):
        return self.calendar_day.date if self.calendar_day else None

    @classmethod
    def search_date(cls, name, clause):
        return [('calendar_day.date', *clause[1:])]

    @classmethod
    def __register__(cls, module_name):
        table_h = cls.__table_handler__(module_name)
        company_exists = table_h.column_exist('company')
        super().__register__(module_name)
        if not company_exists:
            cursor = Transaction().connection.cursor()
            cursor.execute(*cls.__table__().delete())
            table_h.not_null_action('company', action='add')


class SalesForecastManualLine(ModelSQL, ModelView):
    'Sales Forecast Manual Line'
    __name__ = 'sales.forecast.manual_line'

    product_template = fields.Many2One(
        'product.template', 'Product', required=True, ondelete='CASCADE',
        domain=[
            ('sales_forecast_method', '=', 'manual'),
            ('salable', '=', True),
            ('type', '=', 'goods'),
            ('active', '=', True),
        ])
    week_start = fields.Date('Week', required=True)
    package = fields.Many2One('product.package', 'Forecast Package',
        readonly=True, ondelete='SET NULL')
    units = fields.Float('Units')
    boxes = fields.Float('Boxes')
    pallets = fields.Float('Pallets')
    updated_at = fields.DateTime('Last Update', required=True, readonly=True)

    week_label = fields.Function(fields.Char('Week'), 'get_week_label')

    def get_week_label(self, name):
        if self.week_start:
            year, week, _ = self.week_start.isocalendar()
            return f'{week:02d}/{year % 100:02d}'

    @classmethod
    def __setup__(cls):
        super().__setup__()
        table = cls.__table__()
        cls._sql_constraints = [
            ('manual_product_week_unique',
                Unique(table, table.product_template, table.week_start),
                'sales_forecast.msg_manual_product_week_unique'),
        ]
        cls._order.insert(0, ('week_start', 'ASC'))

    @staticmethod
    def _current_week_start():
        today = Pool().get('ir.date').today()
        return today - timedelta(days=today.weekday())

    @classmethod
    def default_week_start(cls):
        week = cls._current_week_start()
        template_id = Transaction().context.get('default_product_template')
        if template_id:
            previous = cls.search([
                    ('product_template', '=', template_id),
                    ], order=[('week_start', 'DESC')], limit=1)
            if previous:
                week = max(week, previous[0].week_start + timedelta(days=7))
        return week

    @staticmethod
    def default_product_template():
        return Transaction().context.get('default_product_template')

    @fields.depends('product_template', '_parent_product_template.id')
    def on_change_product_template(self):
        self.week_start = self._current_week_start()
        if self.product_template:
            previous = self.search([
                    ('product_template', '=', self.product_template.id),
                    ], order=[('week_start', 'DESC')], limit=1)
            if previous:
                self.week_start = max(self.week_start,
                    previous[0].week_start + timedelta(days=7))

    @classmethod
    def _normalized_quantities(cls, template, values, current=None):
        quantity_fields = [
            field_name for field_name in ('units', 'boxes', 'pallets')
            if field_name in values and values[field_name] is not None]
        source = quantity_fields[0] if quantity_fields else None
        if current:
            changed_fields = [
                field_name for field_name in quantity_fields
                if values[field_name] != getattr(current, field_name)]
            if len(changed_fields) == 1:
                source = changed_fields[0]
        if source is None and current:
            source = 'units'
            values = {**values, 'units': current.units}
        if source is None:
            raise UserError(gettext(
                'sales_forecast.msg_missing_manual_quantity',
                product=template.rec_name))
        quantities = _convert_packaging_quantities(
            template, **{source: values[source]})
        package, _, _ = _packaging_capabilities(template)
        quantities['package'] = package.id if package else None
        return quantities

    @classmethod
    def create(cls, vlist):
        pool = Pool()
        Template = pool.get('product.template')
        expected_dates = {}
        normalized = []
        for values in sorted(
                (values.copy() for values in vlist),
                key=lambda values: (
                    values.get('product_template', 0),
                    values.get('week_start') or date.min)):
            template = Template(values['product_template'])
            if template.id not in expected_dates:
                previous = cls.search([
                        ('product_template', '=', template.id),
                        ], order=[('week_start', 'DESC')], limit=1)
                expected_dates[template.id] = max(
                    previous[0].week_start + timedelta(days=7)
                    if previous else cls._current_week_start(),
                    cls._current_week_start())
            week_start = values.get('week_start') or expected_dates[template.id]
            if week_start != expected_dates[template.id]:
                raise UserError(gettext(
                    'sales_forecast.msg_manual_week_sequence',
                    product=template.rec_name,
                    week=expected_dates[template.id]))
            if week_start < cls._current_week_start():
                raise UserError(gettext(
                    'sales_forecast.msg_manual_week_in_past'))
            values['week_start'] = week_start
            values.update(cls._normalized_quantities(template, values))
            values['updated_at'] = datetime.now()
            normalized.append(values)
            expected_dates[template.id] += timedelta(days=7)
        records = super().create(normalized)
        Template.write(list({record.product_template for record in records}), {
                'sales_forecast_manual_updated_at': datetime.now(),
                })
        return records

    @classmethod
    def write(cls, *args):
        actions = iter(args)
        new_args = []
        templates = set()
        for records, values in zip(actions, actions):
            templates.update(record.product_template for record in records)
            if any(record.week_start < cls._current_week_start()
                    for record in records):
                raise UserError(gettext(
                    'sales_forecast.msg_manual_week_in_past'))
            if 'product_template' in values or 'week_start' in values:
                raise UserError(gettext(
                    'sales_forecast.msg_manual_week_readonly'))
            values = values.copy()
            if any(field in values for field in ('units', 'boxes', 'pallets')):
                if len(records) != 1:
                    raise UserError(gettext(
                        'sales_forecast.msg_manual_single_update'))
                values.update(cls._normalized_quantities(
                    records[0].product_template, values, current=records[0]))
            values['updated_at'] = datetime.now()
            new_args.extend([records, values])
        super().write(*new_args)
        Pool().get('product.template').write(list(templates), {
                'sales_forecast_manual_updated_at': datetime.now(),
                })

    @classmethod
    def delete(cls, records):
        by_template = {}
        for record in records:
            by_template.setdefault(record.product_template.id, set()).add(
                record.id)
        for template_id, record_ids in by_template.items():
            lines = cls.search([
                    ('product_template', '=', template_id),
                    ], order=[('week_start', 'ASC')])
            first_deleted = next(
                (index for index, line in enumerate(lines)
                    if line.id in record_ids), None)
            if (first_deleted is not None
                    and any(line.id not in record_ids
                        for line in lines[first_deleted:])):
                raise UserError(gettext(
                    'sales_forecast.msg_manual_delete_sequence'))
        super().delete(records)
        Template = Pool().get('product.template')
        templates = Template.search([('id', 'in', list(by_template))])
        if templates:
            Template.write(templates, {
                    'sales_forecast_manual_updated_at': datetime.now(),
                    })


class SalesForecastCalculationProduct(ModelSQL):
    'Sales Forecast Calculation Product'
    __name__ = 'sales.forecast.calculation-product.template'

    calculation = fields.Many2One('sales.forecast.calculation',
        'Calculation', required=True, ondelete='CASCADE')
    product_template = fields.Many2One('product.template', 'Product',
        required=True, ondelete='CASCADE')


class SalesForecastSelectedProduct(ModelSQL):
    'Sales Forecast Selected Product'
    __name__ = 'sales.forecast-product.template'

    forecast = fields.Many2One('sales.forecast', 'Forecast',
        required=True, ondelete='CASCADE')
    product_template = fields.Many2One('product.template', 'Product',
        required=True, ondelete='CASCADE')


class SalesForecastCalculationCategoryIncrease(ModelSQL, ModelView):
    'Sales Forecast Calculation Category Increase'
    __name__ = 'sales.forecast.calculation.category_increase'

    calculation = fields.Many2One('sales.forecast.calculation',
        'Calculation', required=True, ondelete='CASCADE')
    category = fields.Many2One('product.category', 'Category',
        required=True, ondelete='RESTRICT')
    percentage = fields.Float('Increase %',
        help='Enter a percentage, for example 20 for 20%%.')

    @classmethod
    def __setup__(cls):
        super().__setup__()
        table = cls.__table__()
        cls._sql_constraints = [
            ('calculation_category_unique',
                Unique(table, table.calculation, table.category),
                'sales_forecast.msg_calculation_category_unique'),
        ]


class SalesForecastCalculationProductIncrease(ModelSQL, ModelView):
    'Sales Forecast Calculation Product Increase'
    __name__ = 'sales.forecast.calculation.product_increase'

    calculation = fields.Many2One('sales.forecast.calculation',
        'Calculation', required=True, ondelete='CASCADE')
    product_template = fields.Many2One('product.template', 'Product',
        required=True, ondelete='RESTRICT',
        domain=[
            ('sales_forecast_method', '=', 'general'),
            ('salable', '=', True),
            ('type', '=', 'goods'),
            ('active', '=', True),
        ])
    percentage = fields.Float('Increase %', required=True,
        help='Enter a percentage, for example 20 for 20%%.')


class SalesForecastCalculation(ModelSingleton, ModelSQL, ModelView):
    'Sales Forecast Calculation'
    __name__ = 'sales.forecast.calculation'

    company = fields.Many2One('company.company', 'Company', required=True)
    calendar = fields.Many2One('sales.forecast.calendar', 'Calendar')
    family_category = fields.Many2One('product.category', 'Product Family Root',
        context={'company': Eval('company', -1)},
        help='Only categories under this root are used as product families.')
    general_increase_percentage = fields.Float('General Increase %')
    start_date = fields.Date('Start Date')
    end_date = fields.Date('End Date')
    product_templates = fields.Many2Many(
        'sales.forecast.calculation-product.template', 'calculation',
        'product_template', 'Products',
        domain=[
            ('salable', '=', True),
            ('type', '=', 'goods'),
            ('active', '=', True),
        ],
        context={
            'company': Eval('company', -1),
        })
    category_increase_lines = fields.One2Many(
        'sales.forecast.calculation.category_increase', 'calculation',
        'Category Increases')
    product_increase_lines = fields.One2Many(
        'sales.forecast.calculation.product_increase', 'calculation',
        'Product Increases')
    generated_forecasts = fields.One2Many(
        'sales.forecast', 'calculation', 'Generated Forecasts', readonly=True,
        order=[('computed_at', 'DESC NULLS LAST'), ('id', 'DESC')])
    last_general_calculated_at = fields.DateTime(
        'Last General Calculation', readonly=True)
    last_key_accounts_calculated_at = fields.DateTime(
        'Last Key Accounts Calculation', readonly=True)
    last_new_product_calculated_at = fields.DateTime(
        'Last New Product Calculation', readonly=True)
    last_manual_calculated_at = fields.DateTime(
        'Last Manual Calculation', readonly=True)
    last_calculated_at = fields.Function(
        fields.DateTime('Last Calculation'), 'get_last_calculated_at')

    refresh_parameters = fields.Boolean('Refresh Forecast Parameters',
        help='Refresh sales history, weekday distributions and calendar results. '
            'Otherwise reuse the existing parameters.')

    def get_last_calculated_at(self, name):
        return max((value for value in (
                    self.last_general_calculated_at,
                    self.last_key_accounts_calculated_at,
                    self.last_new_product_calculated_at,
                    self.last_manual_calculated_at,
                    ) if value is not None), default=None)

    @staticmethod
    def default_company():
        return Transaction().context.get('company')

    @staticmethod
    def default_calendar():
        calendars = Pool().get('sales.forecast.calendar').active_calendars()
        if calendars:
            return calendars[0].id

    @classmethod
    def __setup__(cls):
        super().__setup__()
        cls._buttons.update({
                'calculate_all': {'icon': 'tryton-refresh'},
                'calculate_general': {'icon': 'tryton-refresh'},
                'calculate_key_accounts': {'icon': 'tryton-refresh'},
                'calculate_new_product': {'icon': 'tryton-refresh'},
                'calculate_manual': {'icon': 'tryton-refresh'},
                'load_general_categories': {'icon': 'tryton-refresh'},
                'open_manual_lines': {'icon': 'tryton-launch'},
                })

    @classmethod
    def create(cls, vlist):
        records = super().create(vlist)
        cls._sync_general_categories(records)
        return records

    @classmethod
    def write(cls, *args):
        records = []
        actions = iter(args)
        for action_records, values in zip(actions, actions):
            if ({'family_category', 'product_templates'}.intersection(values)
                    and 'category_increase_lines' not in values):
                records.extend(action_records)
        super().write(*args)
        cls._sync_general_categories(list({record.id: record
                    for record in records}.values()))

    @classmethod
    def _general_templates(cls, record):
        Forecast = Pool().get('sales.forecast')
        return Forecast.filtered_templates(
            method='general',
            product_templates=list(record.product_templates))

    @classmethod
    def _sync_general_categories(cls, records):
        CategoryIncrease = Pool().get(
            'sales.forecast.calculation.category_increase')
        Forecast = Pool().get('sales.forecast')
        goods_categories = {
            category.id
            for template in Forecast.filtered_templates()
            if (template.sales_forecast_method == 'general'
                or (template.sales_forecast_method == 'key_accounts'
                    and template.sales_forecast_key_account_source == 'mixed'))
            for category in template.categories_all
            }
        for record in records:
            # Clients address singletons as id 1, even when the stored id differs.
            record = cls.get_singleton()
            if not record:
                continue
            category_ids = {
                category.id
                for template in Forecast.filtered_templates(
                    product_templates=list(record.product_templates))
                if (template.sales_forecast_method == 'general'
                    or (template.sales_forecast_method == 'key_accounts'
                        and template.sales_forecast_key_account_source == 'mixed'))
                for category in template.categories_all
                }
            allowed = goods_categories.copy()
            if record.family_category:
                Category = Pool().get('product.category')
                allowed &= {c.id for c in Category.search([
                        ('parent', 'child_of', [record.family_category.id]),
                        ])} - {record.family_category.id}
            category_ids &= allowed
            obsolete = [line for line in record.category_increase_lines
                if line.category.id not in allowed]
            if obsolete:
                CategoryIncrease.delete(obsolete)
            existing_ids = {
                line.category.id for line in record.category_increase_lines
                if line.category.id in category_ids}
            missing_ids = category_ids - existing_ids
            if missing_ids:
                CategoryIncrease.create([{
                            'calculation': record.id,
                            'category': category_id,
                            'percentage': None,
                            } for category_id in sorted(missing_ids)])

    @classmethod
    @ModelView.button
    def load_general_categories(cls, records):
        cls._sync_general_categories(records)

    @classmethod
    @ModelView.button
    def open_manual_lines(cls, records):
        if not records:
            return
        pool = Pool()
        Action = pool.get('ir.action')
        ModelData = pool.get('ir.model.data')
        Forecast = pool.get('sales.forecast')
        record = records[0]
        templates = Forecast.filtered_templates(
            method='manual',
            product_templates=list(record.product_templates))
        action_id = ModelData.get_id(
            'sales_forecast', 'act_sales_forecast_manual_line')
        action = Action(action_id).get_action_value()
        action['pyson_domain'] = PYSONEncoder().encode([
                ('product_template', 'in', [
                        template.id for template in templates]),
                ])
        action_context = {
            'manual_product_ids': [template.id for template in templates],
            }
        if len(templates) == 1:
            action_context.update({
                    'default_product_template': templates[0].id,
                    })
            ManualLine = pool.get('sales.forecast.manual_line')
            if not ManualLine.search([
                    ('product_template', '=', templates[0].id),
                    ], limit=1):
                view_id = ModelData.get_id('sales_forecast',
                    'view_sales_forecast_manual_line_form')
                action['views'] = [(view_id, 'form')]
                action['res_id'] = None
        action['pyson_context'] = PYSONEncoder().encode(action_context)
        return action

    @classmethod
    def _run_method(cls, records, method):
        Forecast = Pool().get('sales.forecast')
        now = datetime.now()
        field_name = {
            'general': 'last_general_calculated_at',
            'key_accounts': 'last_key_accounts_calculated_at',
            'new_product': 'last_new_product_calculated_at',
            'manual': 'last_manual_calculated_at',
            }[method]
        for record in records:
            record = cls.get_singleton()
            if not record.start_date or not record.end_date:
                raise UserError(gettext(
                    'sales_forecast.msg_missing_calculation_period'))
            if method == 'general':
                cls._validate_general_method(record)
            values = {
                'name': '%s %s - %s' % (
                    FORECAST_METHOD_LABELS.get(method, method),
                    record.start_date,
                    record.end_date),
                'calculation': record.id,
                'company': record.company.id,
                'calendar': record.calendar.id if record.calendar else None,
                'general_increase_percentage':
                    record.general_increase_percentage,
                'start_date': record.start_date,
                'end_date': record.end_date,
                'calculation_method': method,
                'refresh_parameters': record.refresh_parameters,
                'product_templates': [('add', [
                        template.id for template in record.product_templates])],
            }
            forecast, = Forecast.create([values])
            if method in {'general', 'key_accounts'}:
                cls._copy_general_parameters(record, forecast)
            templates = Forecast.filtered_templates(
                method=method,
                product_templates=list(record.product_templates))
            Forecast._prepare_calculation(forecast)
            Forecast._calculate(forecast, templates=templates)
            Forecast.write([forecast], {
                    'state': 'calculated',
                    })
        cls.write(records, {
                field_name: now,
                })

    @classmethod
    def _validate_general_method(cls, record):
        Forecast = Pool().get('sales.forecast')
        for template in cls._general_templates(record):
            Forecast._general_percentage(record, template)

    @classmethod
    def _copy_general_parameters(cls, record, forecast):
        ForecastCategoryIncrease = Pool().get('sales.forecast.category_increase')
        ForecastProductIncrease = Pool().get('sales.forecast.product_increase')
        if record.category_increase_lines:
            ForecastCategoryIncrease.create([{
                        'forecast': forecast.id,
                        'category': line.category.id,
                        'percentage': line.percentage,
                        } for line in record.category_increase_lines
                    if line.percentage is not None])
        if record.product_increase_lines:
            ForecastProductIncrease.create([{
                        'forecast': forecast.id,
                        'product_template': line.product_template.id,
                        'percentage': line.percentage,
                        } for line in record.product_increase_lines])

    @classmethod
    @ModelView.button
    def calculate_all(cls, records):
        for method, _label in FORECAST_METHODS:
            cls._run_method(records, method)

    @classmethod
    @ModelView.button
    def calculate_general(cls, records):
        cls._run_method(records, 'general')

    @classmethod
    @ModelView.button
    def calculate_key_accounts(cls, records):
        cls._run_method(records, 'key_accounts')

    @classmethod
    @ModelView.button
    def calculate_new_product(cls, records):
        cls._run_method(records, 'new_product')

    @classmethod
    @ModelView.button
    def calculate_manual(cls, records):
        cls._run_method(records, 'manual')


class SalesForecast(Workflow, ModelSQL, ModelView):
    'Sales Forecast'
    __name__ = 'sales.forecast'

    name = fields.Char('Name', required=True,
        states={'readonly': Eval('state') != 'draft'})
    calculation_method = fields.Selection(
        FORECAST_METHODS, 'Calculation Method', required=True,
        states={
            'readonly': Eval('state') != 'draft',
            })
    calculation = fields.Many2One(
        'sales.forecast.calculation', 'Calculation Source', readonly=True,
        ondelete='SET NULL')
    company = fields.Many2One('company.company', 'Company', required=True,
        ondelete='CASCADE',
        states={
            'readonly': Eval('state') != 'draft',
            })
    calendar = fields.Many2One('sales.forecast.calendar',
        'Calendar',
        states={
            'readonly': Eval('state') != 'draft',
            })
    general_increase_percentage = fields.Float('General Increase %',
        help='Percentage applied to products using the General Method.',
        states={
            'readonly': Eval('state') != 'draft',
            })
    start_date = fields.Date('Start Date', required=True,
        states={
            'readonly': Eval('state') != 'draft',
            })
    end_date = fields.Date('End Date', required=True,
        states={
            'readonly': Eval('state') != 'draft',
            })
    product_templates = fields.Many2Many(
        'sales.forecast-product.template', 'forecast', 'product_template',
        'Products',
        domain=[
            ('salable', '=', True),
            ('type', '=', 'goods'),
            ('active', '=', True),
        ],
        context={
            'company': Eval('company', -1),
        },
        states={
            'readonly': Eval('state') != 'draft',
            })
    computed_at = fields.DateTime('Computed At', readonly=True)
    last_general_calculated_at = fields.DateTime(
        'Last General Calculation', readonly=True)
    last_key_accounts_calculated_at = fields.DateTime(
        'Last Key Accounts Calculation', readonly=True)
    last_new_product_calculated_at = fields.DateTime(
        'Last New Product Calculation', readonly=True)
    last_manual_calculated_at = fields.DateTime(
        'Last Manual Calculation', readonly=True)
    lines = fields.One2Many('sales.forecast.line', 'forecast',
        'Lines',
        states={
            'readonly': Eval('state') != 'draft',
            })
    category_increase_lines = fields.One2Many(
        'sales.forecast.category_increase', 'forecast',
        'Category Increases',
        states={
            'readonly': Eval('state') != 'draft',
            })
    product_increase_lines = fields.One2Many(
        'sales.forecast.product_increase', 'forecast',
        'Product Increases',
        states={
            'readonly': Eval('state') != 'draft',
            })
    state = fields.Selection([
        ('draft', 'Draft'),
        ('calculated', 'Calculated'),
        ('cancelled', 'Cancelled'),
    ], 'State', required=True, readonly=True)

    refresh_parameters = fields.Boolean('Refresh Forecast Parameters',
        states={'readonly': Eval('state') != 'draft'},
        help='Refresh sales history, weekday distributions and calendar results. '
            'Otherwise reuse the existing parameters.')

    @classmethod
    def __setup__(cls):
        super().__setup__()
        cls._order = [('computed_at', 'DESC NULLS LAST'), ('id', 'DESC')]
        cls._transitions |= set((
            ('draft', 'calculated'),
            ('calculated', 'calculated'),
            ('calculated', 'draft'),
            ('draft', 'cancelled'),
            ('calculated', 'cancelled'),
            ('cancelled', 'draft'),
        ))
        cls._buttons.update({
            'calculate': {
                'icon': 'tryton-refresh',
                'invisible': Eval('state') == 'cancelled',
                'depends': ['state'],
            },
            'open_month_lines': {
                'icon': 'tryton-launch',
                'invisible': Eval('state') != 'calculated',
                'depends': ['state'],
            },
            'open_week_lines': {
                'icon': 'tryton-launch',
                'invisible': Eval('state') != 'calculated',
                'depends': ['state'],
            },
            'update_current_year_sales': {
                'icon': 'tryton-refresh',
                'invisible': Eval('state') != 'calculated',
                'depends': ['state'],
            },
            'open_daily_lines': {
                'icon': 'tryton-launch',
                'invisible': Eval('state') != 'calculated',
                'depends': ['state'],
            },
            'draft': {
                'icon': 'tryton-back',
                'invisible': Eval('state') != 'cancelled',
                'depends': ['state'],
            },
            'cancel': {
                'icon': 'tryton-cancel',
                'invisible': Eval('state') == 'cancelled',
                'depends': ['state'],
            },
        })

    @staticmethod
    def default_company():
        return Transaction().context.get('company')

    @staticmethod
    def default_state():
        return 'draft'

    @staticmethod
    def default_calculation_method():
        return 'general'

    @classmethod
    def __register__(cls, module_name):
        cursor = Transaction().connection.cursor()
        table = cls.__table__()
        super().__register__(module_name)
        cursor.execute(*table.update(
                columns=[table.calculation_method],
                values=['general'],
                where=((table.calculation_method == Null)
                    | (table.calculation_method == 'all_methods'))))

    @staticmethod
    def default_start_date():
        today = date.today()
        return today - timedelta(days=today.weekday())

    @staticmethod
    def default_end_date():
        start = SalesForecast.default_start_date()
        return start + timedelta(days=27)

    @classmethod
    def write(cls, *args):
        if Transaction().check_access:
            for records in args[::2]:
                if any(record.state == 'calculated' for record in records):
                    raise UserError(gettext(
                        'sales_forecast.msg_calculated_readonly'))
        super().write(*args)

    @classmethod
    def validate(cls, forecasts):
        super().validate(forecasts)
        for forecast in forecasts:
            if forecast.start_date > forecast.end_date:
                raise UserError(gettext(
                    'sales_forecast.msg_invalid_period'))

    @classmethod
    @ModelView.button
    @Workflow.transition('draft')
    def draft(cls, forecasts):
        pass

    @classmethod
    @ModelView.button
    @Workflow.transition('cancelled')
    def cancel(cls, forecasts):
        pass

    @classmethod
    @ModelView.button
    @Workflow.transition('calculated')
    def calculate(cls, forecasts):
        for forecast in forecasts:
            method = forecast.calculation_method
            if method == 'general':
                cls._validate_general_method(forecast)
            cls._prepare_calculation(forecast)
            cls._calculate_method(forecast, method)

    @classmethod
    def _prepare_calculation(cls, forecast):
        pool = Pool()
        Calendar = pool.get('sales.forecast.calendar')
        History = pool.get('sales.forecast.sale_history')
        Distribution = pool.get('sales.forecast.distribution')
        EventResult = pool.get('sales.forecast.event_result')
        calendars = ([forecast.calendar] if forecast.calendar
            else Calendar.active_calendars())
        refresh = forecast.refresh_parameters
        if refresh or History.last_date(forecast.company) is None:
            History.update_history(company=forecast.company)
        templates = cls.filtered_templates(
            method=forecast.calculation_method,
            product_templates=list(forecast.product_templates))
        distributions = Distribution.search([
                ('company', '=', forecast.company.id),
                ('product_template', 'in', [t.id for t in templates]),
                ])
        ready = {line.product_template.id for line in distributions}
        with Transaction().set_context(company=forecast.company.id,
                sales_forecast_history_ready=True):
            if (refresh or any(t.id not in ready for t in templates)
                    or any(not c.distribution_calculated_at for c in calendars)):
                Calendar.calculate_distributions(calendars)
            pending = []
            for calendar in calendars:
                has_events = any(day.day_type != 'festive'
                    for day in calendar.day_lines)
                if (refresh or not calendar.events_calculated_at
                        or (has_events and not EventResult.search([
                            ('company', '=', forecast.company.id),
                            ('calendar_day.calendar', '=', calendar.id),
                            ], limit=1))):
                    pending.append(calendar)
            if pending:
                Calendar.calculate_event_results(pending)

    @classmethod
    @ModelView.button_action('sales_forecast.act_sales_forecast_monthly_line')
    def open_month_lines(cls, forecasts):
        pass

    @classmethod
    @ModelView.button_action('sales_forecast.act_sales_forecast_daily_line')
    def open_daily_lines(cls, forecasts):
        pass

    @classmethod
    @ModelView.button_action('sales_forecast.act_sales_forecast_line')
    def open_week_lines(cls, forecasts):
        pass

    @classmethod
    @ModelView.button
    def update_current_year_sales(cls, forecasts):
        pool = Pool()
        SaleHistory = pool.get('sales.forecast.sale_history')
        Line = pool.get('sales.forecast.line')
        DailyLine = pool.get('sales.forecast.daily_line')

        companies = {forecast.company for forecast in forecasts}
        for company in companies:
            SaleHistory.update_history(company=company)
        for forecast in forecasts:
            lines = Line.search([
                ('forecast', '=', forecast.id),
            ])
            if lines:
                cls._update_current_year_fields(lines)
            daily_lines = DailyLine.search([
                ('forecast', '=', forecast.id),
            ])
            if daily_lines:
                cls._update_current_year_daily_fields(daily_lines)
                cls._refresh_monthly_lines(forecast, daily_lines)

    @classmethod
    def _calculate(cls, forecast, templates=None):
        pool = Pool()
        Line = pool.get('sales.forecast.line')
        DailyLine = pool.get('sales.forecast.daily_line')
        MonthlyLine = pool.get('sales.forecast.monthly_line')

        cls.lock()
        if templates is None:
            templates = cls.filtered_templates(
                method=forecast.calculation_method)

        existing_lines = Line.search([
            ('forecast', '=', forecast.id),
        ])
        if existing_lines:
            Line.delete(existing_lines)
        existing_daily_lines = DailyLine.search([
            ('forecast', '=', forecast.id),
        ])
        if existing_daily_lines:
            DailyLine.delete(existing_daily_lines)
        existing_monthly_lines = MonthlyLine.search([
            ('forecast', '=', forecast.id),
        ])
        if existing_monthly_lines:
            MonthlyLine.delete(existing_monthly_lines)
        current_year_quantities = cls._current_year_quantity_map(
            forecast.company, [
                template.id for template in templates
            ], forecast.start_date, forecast.end_date)
        to_create = []
        for template in templates:
            for week_start in cls._iter_weeks(
                    forecast.start_date, forecast.end_date):
                quantity, adjustment, base_quantity = cls._compute_quantity(
                    forecast, template, week_start)
                packaging_quantities = _convert_packaging_quantities(
                    template, units=round(quantity, 2), round_up=True)
                package, _, _ = _packaging_capabilities(template)
                to_create.append({
                    'forecast': forecast.id,
                    'product_template': template.id,
                    'product': (template.products[0].id
                        if len(template.products) == 1 else None),
                    'package': package.id if package else None,
                    'week_start': week_start,
                    'method': template.sales_forecast_method,
                    'origin': ('manual'
                        if template.sales_forecast_method == 'manual'
                        else 'calculated'),
                    'base_quantity': round(base_quantity, 2),
                    'calendar_adjustment': round(adjustment, 4),
                    'quantity': packaging_quantities['units'],
                    'units': packaging_quantities['units'],
                    'boxes': packaging_quantities['boxes'],
                    'pallets': packaging_quantities['pallets'],
                    'current_year_quantity': cls._current_year_week_quantity(
                        template.id, week_start, current_year_quantities),
                    'previous_year_1_quantity':
                        cls._previous_year_week_quantity(
                            forecast.company, template, week_start),
                    'previous_year_2_quantity':
                        cls._previous_year_week_quantity(
                            forecast.company, template,
                            previous_year_date(week_start)),
                    'previous_year_3_quantity':
                        cls._previous_year_week_quantity(
                            forecast.company, template,
                            previous_year_date(week_start, 2)),
                })
        if to_create:
            lines = Line.create(to_create)
            daily_lines = DailyLine.create(cls._daily_line_values(lines))
            cls._refresh_monthly_lines(forecast, daily_lines)
        cls.write([forecast], {
            'computed_at': datetime.now(),
        })

    @classmethod
    def _calculate_method(cls, forecast, method):
        pool = Pool()
        Line = pool.get('sales.forecast.line')
        DailyLine = pool.get('sales.forecast.daily_line')
        MonthlyLine = pool.get('sales.forecast.monthly_line')

        templates = cls.filtered_templates(
            method=method,
            product_templates=list(forecast.product_templates))
        method_lines = Line.search([
            ('forecast', '=', forecast.id),
            ('method', '=', method),
        ])
        if method_lines:
            daily_lines = DailyLine.search([
                ('line', 'in', [line.id for line in method_lines]),
            ])
            if daily_lines:
                DailyLine.delete(daily_lines)
            Line.delete(method_lines)
        current_year_quantities = cls._current_year_quantity_map(
            forecast.company, [
                template.id for template in templates
            ], forecast.start_date, forecast.end_date)
        to_create = []
        for template in templates:
            for week_start in cls._iter_weeks(
                    forecast.start_date, forecast.end_date):
                quantity, adjustment, base_quantity = cls._compute_quantity(
                    forecast, template, week_start)
                packaging_quantities = _convert_packaging_quantities(
                    template, units=round(quantity, 2), round_up=True)
                package, _, _ = _packaging_capabilities(template)
                to_create.append({
                    'forecast': forecast.id,
                    'product_template': template.id,
                    'product': (template.products[0].id
                        if len(template.products) == 1 else None),
                    'package': package.id if package else None,
                    'week_start': week_start,
                    'method': template.sales_forecast_method,
                    'origin': ('manual'
                        if template.sales_forecast_method == 'manual'
                        else 'calculated'),
                    'base_quantity': round(base_quantity, 2),
                    'calendar_adjustment': round(adjustment, 4),
                    'quantity': packaging_quantities['units'],
                    'units': packaging_quantities['units'],
                    'boxes': packaging_quantities['boxes'],
                    'pallets': packaging_quantities['pallets'],
                    'current_year_quantity': cls._current_year_week_quantity(
                        template.id, week_start, current_year_quantities),
                    'previous_year_1_quantity':
                        cls._previous_year_week_quantity(
                            forecast.company, template, week_start),
                    'previous_year_2_quantity':
                        cls._previous_year_week_quantity(
                            forecast.company, template,
                            previous_year_date(week_start)),
                    'previous_year_3_quantity':
                        cls._previous_year_week_quantity(
                            forecast.company, template,
                            previous_year_date(week_start, 2)),
                })
        if to_create:
            Line.create(to_create)
        all_lines = Line.search([
            ('forecast', '=', forecast.id),
        ])
        existing_daily_lines = DailyLine.search([
            ('forecast', '=', forecast.id),
        ])
        if existing_daily_lines:
            DailyLine.delete(existing_daily_lines)
        existing_monthly_lines = MonthlyLine.search([
            ('forecast', '=', forecast.id),
        ])
        if existing_monthly_lines:
            MonthlyLine.delete(existing_monthly_lines)
        if all_lines:
            daily_lines = DailyLine.create(cls._daily_line_values(all_lines))
            cls._refresh_monthly_lines(forecast, daily_lines)
        field_name = {
            'general': 'last_general_calculated_at',
            'key_accounts': 'last_key_accounts_calculated_at',
            'new_product': 'last_new_product_calculated_at',
            'manual': 'last_manual_calculated_at',
        }[method]
        cls.write([forecast], {
                'state': 'calculated',
                'computed_at': datetime.now(),
                field_name: datetime.now(),
                })

    @classmethod
    def _validate_general_method(cls, forecast):
        templates = cls.filtered_templates(method='general',
            product_templates=list(forecast.product_templates))
        for template in templates:
            cls._general_percentage(forecast, template)

    @classmethod
    def filtered_templates(cls, method=None, product_templates=None):
        Template = Pool().get('product.template')
        domain = [
            ('salable', '=', True),
            ('type', '=', 'goods'),
            ('active', '=', True),
        ]
        if method:
            domain.append(('sales_forecast_method', '=', method))
        if product_templates:
            domain.append(('id', 'in', [template.id for template in product_templates]))
        return Template.search(domain, order=[('name', 'ASC')])

    @classmethod
    def _current_year_quantity_map(cls, company, product_template_ids,
            start_date, end_date):
        SaleHistory = Pool().get('sales.forecast.sale_history')
        return SaleHistory.range_quantity_map(
            company, product_template_ids, start_date, end_date)

    @classmethod
    def _current_year_week_quantity(cls, product_template_id, week_start,
            quantity_map):
        quantity = 0.0
        for offset in range(7):
            quantity += quantity_map.get(
                (product_template_id, week_start + timedelta(days=offset)), 0.0)
        return round(quantity, 2)

    @classmethod
    def _update_current_year_fields(cls, lines):
        Line = Pool().get('sales.forecast.line')
        if not lines:
            return
        product_template_ids = list({
                line.product_template.id for line in lines if line.product_template
                })
        start_date = min(line.week_start for line in lines)
        end_date = max(line.week_end for line in lines)
        current_year_quantities = cls._current_year_quantity_map(
            lines[0].forecast.company, product_template_ids,
            start_date, end_date)
        for line in lines:
            Line.write([line], {
                'current_year_quantity': cls._current_year_week_quantity(
                    line.product_template.id, line.week_start,
                    current_year_quantities),
            })

    @classmethod
    def _update_current_year_daily_fields(cls, daily_lines):
        DailyLine = Pool().get('sales.forecast.daily_line')
        if not daily_lines:
            return
        product_template_ids = list({
                line.product_template.id for line in daily_lines
                if line.product_template
                })
        start_date = min(line.date for line in daily_lines)
        end_date = max(line.date for line in daily_lines)
        current_year_quantities = cls._current_year_quantity_map(
            daily_lines[0].forecast.company, product_template_ids,
            start_date, end_date)
        for line in daily_lines:
            DailyLine.write([line], {
                'current_year_quantity': current_year_quantities.get(
                    (line.product_template.id, line.date), 0.0),
            })

    @classmethod
    def _refresh_monthly_lines(cls, forecast, daily_lines):
        MonthlyLine = Pool().get('sales.forecast.monthly_line')
        existing_monthly_lines = MonthlyLine.search([
            ('forecast', '=', forecast.id),
        ])
        if existing_monthly_lines:
            MonthlyLine.delete(existing_monthly_lines)
        values = cls._monthly_line_values(daily_lines)
        if values:
            MonthlyLine.create(values)

    @classmethod
    def _daily_line_values(cls, lines):
        pool = Pool()
        SaleHistory = pool.get('sales.forecast.sale_history')

        product_template_ids = list({
                line.product_template.id for line in lines if line.product_template
                })
        start_date = min(line.week_start for line in lines) if lines else None
        end_date = max(line.week_end for line in lines) if lines else None
        current_year_quantities = cls._current_year_quantity_map(
            lines[0].forecast.company, product_template_ids,
            start_date, end_date)
        history_dates = set()
        for line in lines:
            for offset in range(7):
                current_date = line.week_start + timedelta(days=offset)
                history_dates.add(previous_year_date(current_date, 1))
                history_dates.add(previous_year_date(current_date, 2))
                history_dates.add(previous_year_date(current_date, 3))
        history_quantities = SaleHistory.quantity_map(
            lines[0].forecast.company, product_template_ids,
            list(history_dates))

        values = []
        for line in lines:
            package, _, _ = _packaging_capabilities(line.product_template)
            daily_quantities = cls._calendar_daily_quantities(
                line.forecast, line.product_template, line.week_start,
                line.base_quantity or 0.0,
                apply_events=line.method != 'manual')
            rounded = {day: round(quantity, 2)
                for day, quantity in daily_quantities.items()}
            if rounded:
                last_day = max(rounded)
                rounded[last_day] = round(rounded[last_day]
                    + line.units - sum(rounded.values()), 2)
            for current_date, quantity in rounded.items():
                quantities = _convert_packaging_quantities(
                    line.product_template,
                    units=round(quantity, 2), round_up=True)
                values.append({
                    'forecast': line.forecast.id,
                    'line': line.id,
                    'product_template': line.product_template.id,
                    'product': line.product.id if line.product else None,
                    'package': package.id if package else None,
                    'date': current_date,
                    'weekday': str(current_date.weekday()),
                    'quantity': quantities['units'],
                    'units': quantities['units'],
                    'boxes': quantities['boxes'],
                    'pallets': quantities['pallets'],
                    'current_year_quantity': current_year_quantities.get(
                        (line.product_template.id, current_date), 0.0),
                    'previous_year_1_quantity': history_quantities.get(
                        (line.product_template.id,
                            previous_year_date(current_date, 1)), 0.0),
                    'previous_year_2_quantity': history_quantities.get(
                        (line.product_template.id,
                            previous_year_date(current_date, 2)), 0.0),
                    'previous_year_3_quantity': history_quantities.get(
                        (line.product_template.id,
                            previous_year_date(current_date, 3)), 0.0),
                })
        return values

    @classmethod
    def _monthly_line_values(cls, daily_lines):
        values = {}
        for daily_line in daily_lines:
            month_start = daily_line.date.replace(day=1)
            key = (daily_line.forecast.id, daily_line.product_template.id, month_start)
            if key not in values:
                values[key] = {
                    'forecast': daily_line.forecast.id,
                    'product_template': daily_line.product_template.id,
                    'product': daily_line.product.id if daily_line.product else None,
                    'package': (daily_line.package.id
                        if daily_line.package else None),
                    'month_start': month_start,
                    'method': daily_line.line.method if daily_line.line else None,
                    'quantity': 0.0,
                    'units': 0.0,
                    'boxes': 0.0,
                    'pallets': 0.0,
                    'current_year_quantity': 0.0,
                    'previous_year_1_quantity': 0.0,
                    'previous_year_2_quantity': 0.0,
                    'previous_year_3_quantity': 0.0,
                }
            values[key]['quantity'] += daily_line.quantity or 0.0
            values[key]['units'] += daily_line.units or 0.0
            for name in ('boxes', 'pallets'):
                amount = getattr(daily_line, name)
                if values[key][name] is None or amount is None:
                    values[key][name] = None
                else:
                    values[key][name] += amount
            values[key]['current_year_quantity'] += (
                daily_line.current_year_quantity or 0.0)
            values[key]['previous_year_1_quantity'] += (
                daily_line.previous_year_1_quantity or 0.0)
            values[key]['previous_year_2_quantity'] += (
                daily_line.previous_year_2_quantity or 0.0)
            values[key]['previous_year_3_quantity'] += (
                daily_line.previous_year_3_quantity or 0.0)
        Template = Pool().get('product.template')
        for value in values.values():
            value.update(_convert_packaging_quantities(
                Template(value['product_template']), units=value['units'],
                round_up=True))
        return [{
                **value,
                'quantity': round(value['quantity'], 2),
                'units': round(value['units'], 2),
                'boxes': (round(value['boxes'], 2)
                    if value['boxes'] is not None else None),
                'pallets': (round(value['pallets'], 2)
                    if value['pallets'] is not None else None),
                'current_year_quantity': round(value['current_year_quantity'], 2),
                'previous_year_1_quantity': round(
                    value['previous_year_1_quantity'], 2),
                'previous_year_2_quantity': round(
                    value['previous_year_2_quantity'], 2),
                'previous_year_3_quantity': round(
                    value['previous_year_3_quantity'], 2),
                } for value in values.values()]

    @classmethod
    def _iter_weeks(cls, start_date, end_date):
        current = start_date - timedelta(days=start_date.weekday())
        while current <= end_date:
            yield current
            current += timedelta(days=7)

    @classmethod
    def _compute_quantity(cls, forecast, template, week_start):
        method = template.sales_forecast_method
        if method == 'manual':
            quantity = cls._manual_quantity(template, week_start)
            daily = cls._calendar_daily_quantities(
                forecast, template, week_start, quantity, apply_events=False)
            return sum(daily.values()), 1.0, quantity
        if method == 'general':
            quantity = cls._compute_general_quantity(
                forecast, template, week_start)
        elif method == 'key_accounts':
            quantity = cls._compute_key_account_quantity(
                forecast, template, week_start)
        elif method == 'new_product':
            quantity = cls._compute_new_product_quantity(
                forecast, template, week_start)
        adjusted, adjustment = cls._calendar_week_quantity(
            forecast, template, week_start, quantity)
        return adjusted, adjustment, quantity

    @classmethod
    def _manual_quantity(cls, template, week_start):
        ManualLine = Pool().get('sales.forecast.manual_line')
        lines = ManualLine.search([
                ('product_template', '=', template.id),
                ('week_start', '=', week_start),
                ], limit=1)
        if lines:
            return lines[0].units
        raise UserError(gettext(
            'sales_forecast.msg_missing_manual_week',
            product=template.rec_name,
            week=week_start))

    @classmethod
    def _weekday_percentages(cls, company, template):
        Distribution = Pool().get('sales.forecast.distribution')
        lines = Distribution.search([
                ('company', '=', company.id),
                ('product_template', '=', template.id),
                ])
        percentages = {
            line.weekday: (line.percentage or 0.0) / 100.0
            for line in lines
            }
        total = sum(percentages.values())
        if len(percentages) != 7 or total <= 0:
            raise UserError(gettext(
                'sales_forecast.msg_missing_distribution',
                product=template.rec_name))
        return {
            str(index): percentages[str(index)] / total
            for index in range(7)
            }

    @classmethod
    def _calendar_daily_quantities(cls, forecast, template, week_start,
            quantity, apply_events=True):
        quantity = max(0.0, quantity)
        # Explicit orders and customer quantities take precedence over events.
        # Only the General fallback of the mixed method applies event forecasts.
        if apply_events and template.sales_forecast_method == 'key_accounts':
            apply_events = (
                template.sales_forecast_key_account_source == 'mixed'
                and not cls._draft_orders_quantity(
                    forecast.company, template, week_start))
        percentages = cls._weekday_percentages(forecast.company, template)
        days = {}
        if forecast.calendar:
            days = {
                line.date: line
                for line in forecast.calendar.day_lines
                if week_start <= line.date <= week_start + timedelta(days=6)
                }
        event_quantities = {}
        if apply_events and forecast.calendar:
            EventResult = Pool().get('sales.forecast.event_result')
            results = EventResult.search([
                    ('calendar_day.calendar', '=', forecast.calendar.id),
                    ('calendar_day.date', '>=', week_start),
                    ('calendar_day.date', '<=', week_start + timedelta(days=6)),
                    ('company', '=', forecast.company.id),
                    ('product_template', '=', template.id),
                    ])
            for result in results:
                event_quantities.setdefault(result.calendar_day.date, 0.0)
                event_quantities[result.calendar_day.date] += result.units or 0.0

        weights = {}
        for offset in range(7):
            current = week_start + timedelta(days=offset)
            day = days.get(current)
            weekday = ('6' if (day and day.day_type == 'festive')
                or current.weekday() == 6 else str(offset))
            weights[current] = percentages[weekday]
        if not apply_events:
            total = sum(weights.values())
            weights = {day: weight / total if total else 1 / 7
                for day, weight in weights.items()}
        values = {}
        for offset in range(7):
            current = week_start + timedelta(days=offset)
            if current < forecast.start_date or current > forecast.end_date:
                continue
            if current in event_quantities:
                values[current] = max(0.0, event_quantities[current])
                continue
            values[current] = max(0.0, quantity * weights[current])
        return values

    @classmethod
    def _calendar_week_quantity(cls, forecast, template, week_start,
            quantity):
        daily_quantities = cls._calendar_daily_quantities(
            forecast, template, week_start, quantity)
        adjusted = sum(daily_quantities.values())
        adjustment = adjusted / quantity if quantity else 1.0
        return adjusted, adjustment

    @classmethod
    def _compute_general_quantity(cls, forecast, template, week_start):
        previous_quantity = cls._previous_year_week_quantity(
            forecast.company, template, week_start)
        percentage = cls._general_percentage(forecast, template)
        return previous_quantity * (1 + percentage)

    @classmethod
    def _general_percentage(cls, forecast, template):
        for line in forecast.product_increase_lines:
            if line.product_template == template:
                return line.percentage
        template_categories = set(template.categories_all)
        categories = set(template_categories)
        for category in template_categories:
            while category.parent:
                category = category.parent
                categories.add(category)
        percentages = {line.percentage
            for line in forecast.category_increase_lines
            if line.category in categories and line.percentage is not None}
        if len(percentages) > 1:
            raise UserError(gettext(
                'sales_forecast.msg_conflicting_category_increases',
                product=template.rec_name))
        if percentages:
            return percentages.pop()
        if forecast.general_increase_percentage is None:
            raise UserError(gettext(
                'sales_forecast.msg_missing_general_percentage',
                product=template.rec_name))
        return forecast.general_increase_percentage

    @classmethod
    def _previous_year_week_quantity(cls, company, template, week_start):
        SaleHistory = Pool().get('sales.forecast.sale_history')
        previous_start = previous_year_date(week_start)
        previous_end = previous_start + timedelta(days=6)
        return SaleHistory.quantity_sum(
            company, template, previous_start, previous_end)

    @classmethod
    def _compute_key_account_quantity(cls, forecast, template, week_start):
        source = template.sales_forecast_key_account_source
        if source == 'customer_forecast':
            trucks = template.sales_forecast_customer_trucks_per_week
            pallets_per_truck = (
                template.sales_forecast_customer_pallets_per_truck)
            if not trucks or not pallets_per_truck:
                raise UserError(gettext(
                    'sales_forecast.msg_missing_key_account_parameters',
                    product=template.rec_name))
            _package, package_quantity, boxes_per_pallet = (
                _packaging_capabilities(template))
            if not package_quantity or not boxes_per_pallet:
                raise UserError(gettext(
                    'sales_forecast.msg_missing_forecast_packaging',
                    product=template.rec_name))
            pallets = trucks * pallets_per_truck
            return _convert_packaging_quantities(
                template, pallets=pallets)['units']

        orders_quantity = cls._draft_orders_quantity(
            forecast.company, template, week_start)
        if source == 'orders':
            return orders_quantity
        if orders_quantity:
            return orders_quantity
        return cls._compute_general_quantity(
            forecast, template, week_start)

    @classmethod
    def _compute_new_product_quantity(cls, forecast, template, week_start):
        source = template.sales_forecast_new_product_source or 'average_months'
        reference_months = cls._new_product_reference_months(
            forecast.company, template, forecast.start_date, source)
        if not reference_months:
            raise UserError(gettext(
                'sales_forecast.msg_missing_new_product_history',
                product=template.rec_name))
        week_index = cls._month_week_index(week_start)
        quantities = [
            cls._month_week_quantity(
                forecast.company, template, month, week_index)
            for month in reference_months
            ]
        base = sum(quantities) / len(quantities)
        percentage = (
            template.sales_forecast_new_product_increase_percentage or 0.0)
        return base * (1 + percentage)

    @staticmethod
    def _previous_month(month):
        return (month - timedelta(days=1)).replace(day=1)

    @staticmethod
    def _month_week_index(week_start):
        first_week = week_start.replace(day=1)
        first_week -= timedelta(days=first_week.weekday())
        return max(0, (week_start - first_week).days // 7)

    @classmethod
    def _new_product_reference_months(cls, company, template,
            forecast_start, source):
        SaleHistory = Pool().get('sales.forecast.sale_history')
        today = Pool().get('ir.date').today()
        last_boundary = min(forecast_start.replace(day=1), today.replace(day=1))
        month = cls._previous_month(last_boundary)
        months = []
        for _ in range(11):
            end = month.replace(day=monthrange(month.year, month.month)[1])
            if SaleHistory.quantity_sum(company, template, month, end):
                months.append(month)
                if source == 'last_month':
                    break
            month = cls._previous_month(month)
        return months

    @classmethod
    def _month_week_quantity(cls, company, template, month, week_index):
        SaleHistory = Pool().get('sales.forecast.sale_history')
        first_week = month - timedelta(days=month.weekday())
        raw_start = first_week + timedelta(days=week_index * 7)
        month_end = month.replace(day=monthrange(month.year, month.month)[1])
        start = max(raw_start, month)
        end = min(raw_start + timedelta(days=6), month_end)
        if start > month_end:
            return 0.0
        return SaleHistory.quantity_sum(company, template, start, end)

    @classmethod
    def _draft_orders_quantity(cls, company, template, week_start):
        pool = Pool()
        SaleLine = pool.get('sale.line')
        Uom = pool.get('product.uom')

        week_end = week_start + timedelta(days=6)
        lines = SaleLine.search([
            ('type', '=', 'line'),
            ('product.template', '=', template.id),
            ('sale.company', '=', company.id),
            ('sale.state', 'in', ['draft', 'quotation']),
            ['OR',
                [('sale.shipping_date', '>=', week_start),
                    ('sale.shipping_date', '<=', week_end)],
                [('sale.shipping_date', '=', None),
                    ('sale.sale_date', '>=', week_start),
                    ('sale.sale_date', '<=', week_end)],
                ],
            ('quantity', '>', 0),
        ])
        return sum(
            Uom.compute_qty(
                line.unit, line.quantity, line.product.default_uom, round=False)
            for line in lines)


class SalesForecastCategoryIncrease(ModelSQL, ModelView):
    'Sales Forecast Category Increase'
    __name__ = 'sales.forecast.category_increase'

    forecast = fields.Many2One('sales.forecast', 'Forecast',
        required=True, ondelete='CASCADE')
    category = fields.Many2One('product.category', 'Category',
        required=True, ondelete='RESTRICT')
    percentage = fields.Float('Increase %', required=True)

    @classmethod
    def __setup__(cls):
        super().__setup__()
        table = cls.__table__()
        cls._sql_constraints = [
            ('forecast_category_unique',
                Unique(table, table.forecast, table.category),
                'sales_forecast.msg_forecast_category_unique'),
        ]

    def get_rec_name(self, name):
        return self.category.rec_name


class SalesForecastProductIncrease(ModelSQL, ModelView):
    'Sales Forecast Product Increase'
    __name__ = 'sales.forecast.product_increase'

    forecast = fields.Many2One('sales.forecast', 'Forecast',
        required=True, ondelete='CASCADE')
    product_template = fields.Many2One('product.template', 'Product',
        required=True, ondelete='RESTRICT')
    percentage = fields.Float('Increase %', required=True)

    @classmethod
    def __setup__(cls):
        super().__setup__()
        table = cls.__table__()
        cls._sql_constraints = [
            ('forecast_product_unique',
                Unique(table, table.forecast, table.product_template),
                'sales_forecast.msg_forecast_product_unique'),
        ]

    def get_rec_name(self, name):
        return self.product_template.rec_name


class SalesForecastLine(ModelSQL, ModelView):
    'Sales Forecast Line'
    __name__ = 'sales.forecast.line'

    forecast = fields.Many2One('sales.forecast', 'Forecast',
        required=True, ondelete='CASCADE')
    company = fields.Function(fields.Many2One('company.company', 'Company'),
        'get_company', searcher='search_company')
    product_template = fields.Many2One('product.template', 'Product',
        required=True, ondelete='RESTRICT',
        context={
            'company': Eval('company', -1),
        }, depends={'company'})
    product = fields.Many2One(
        'product.product', 'Variant', ondelete='RESTRICT',
        context={
            'company': Eval('company', -1),
        }, depends={'company'})
    package = fields.Many2One('product.package', 'Forecast Package',
        readonly=True, ondelete='SET NULL')
    week_start = fields.Date('Week Start', required=True)
    week_end = fields.Function(fields.Date('Week End'), 'get_week_end')
    method = fields.Selection(FORECAST_METHODS, 'Method', required=True)
    origin = fields.Selection([
        ('calculated', 'Calculated'),
        ('manual', 'Manual'),
    ], 'Origin', required=True)
    base_quantity = fields.Float('Base Quantity', readonly=True)
    calendar_adjustment = fields.Float('Calendar Adjustment', readonly=True)
    quantity = fields.Float('Quantity', required=True, readonly=True)
    units = fields.Float('Units', readonly=True)
    boxes = fields.Float('Boxes', readonly=True)
    pallets = fields.Float('Pallets', readonly=True)
    current_year_quantity = fields.Float('Current Year', readonly=True)
    previous_year_1_quantity = fields.Float('Previous Year', readonly=True)
    previous_year_2_quantity = fields.Float('Two Years Ago', readonly=True)
    previous_year_3_quantity = fields.Float('Three Years Ago', readonly=True)

    week_label = fields.Function(fields.Char('Week'), 'get_week_label')

    def get_week_label(self, name):
        if self.week_start:
            year, week, _ = self.week_start.isocalendar()
            return f'{week:02d}/{year % 100:02d}'

    @classmethod
    def __setup__(cls):
        super().__setup__()
        table = cls.__table__()
        cls._sql_constraints = [
            ('forecast_product_week_unique',
                Unique(table, table.forecast, table.product_template,
                    table.week_start),
                'sales_forecast.msg_forecast_product_week_unique'),
        ]
        cls._order.insert(0, ('week_start', 'ASC'))
        cls._order.insert(1, ('product_template', 'ASC'))

    def get_rec_name(self, name):
        return '%s - %s' % (self.product_template.rec_name, self.week_start)

    def get_company(self, name):
        if self.forecast and self.forecast.company:
            return self.forecast.company.id

    @classmethod
    def search_company(cls, name, clause):
        return [('forecast.company', *clause[1:])]

    def get_week_end(self, name):
        if self.week_start:
            return self.week_start + timedelta(days=6)


class SalesForecastDailyLine(ModelSQL, ModelView):
    'Sales Forecast Daily Line'
    __name__ = 'sales.forecast.daily_line'

    forecast = fields.Many2One('sales.forecast', 'Forecast',
        required=True, ondelete='CASCADE')
    line = fields.Many2One('sales.forecast.line', 'Forecast Line',
        required=True, ondelete='CASCADE')
    product_template = fields.Many2One('product.template', 'Product',
        required=True, ondelete='RESTRICT')
    product = fields.Many2One('product.product', 'Variant', ondelete='RESTRICT')
    package = fields.Many2One('product.package', 'Forecast Package',
        readonly=True, ondelete='SET NULL')
    date = fields.Date('Date', required=True)
    weekday = fields.Selection(WEEKDAYS, 'Weekday', required=True)
    quantity = fields.Float('Quantity', required=True, readonly=True)
    units = fields.Float('Units', readonly=True)
    boxes = fields.Float('Boxes', readonly=True)
    pallets = fields.Float('Pallets', readonly=True)
    current_year_quantity = fields.Float('Current Year', readonly=True)
    previous_year_1_quantity = fields.Float('Previous Year', readonly=True)
    previous_year_2_quantity = fields.Float('Two Years Ago', readonly=True)
    previous_year_3_quantity = fields.Float('Three Years Ago', readonly=True)

    @classmethod
    def __setup__(cls):
        super().__setup__()
        table = cls.__table__()
        cls._sql_constraints = [
            ('forecast_daily_line_unique',
                Unique(table, table.forecast, table.product_template, table.date),
                'sales_forecast.msg_forecast_daily_line_unique'),
        ]
        cls._order.insert(0, ('date', 'ASC'))
        cls._order.insert(1, ('product_template', 'ASC'))


class SalesForecastMonthlyLine(ModelSQL, ModelView):
    'Sales Forecast Monthly Line'
    __name__ = 'sales.forecast.monthly_line'

    forecast = fields.Many2One('sales.forecast', 'Forecast',
        required=True, ondelete='CASCADE')
    product_template = fields.Many2One('product.template', 'Product',
        required=True, ondelete='RESTRICT')
    product = fields.Many2One('product.product', 'Variant', ondelete='RESTRICT')
    package = fields.Many2One('product.package', 'Forecast Package',
        readonly=True, ondelete='SET NULL')
    month_start = fields.Date('Month Start', required=True)
    month_end = fields.Function(fields.Date('Month End'), 'get_month_end')
    method = fields.Selection(FORECAST_METHODS, 'Method', required=True)
    quantity = fields.Float('Quantity', required=True, readonly=True)
    units = fields.Float('Units', readonly=True)
    boxes = fields.Float('Boxes', readonly=True)
    pallets = fields.Float('Pallets', readonly=True)
    current_year_quantity = fields.Float('Current Year', readonly=True)
    previous_year_1_quantity = fields.Float('Previous Year', readonly=True)
    previous_year_2_quantity = fields.Float('Two Years Ago', readonly=True)
    previous_year_3_quantity = fields.Float('Three Years Ago', readonly=True)

    @classmethod
    def __setup__(cls):
        super().__setup__()
        table = cls.__table__()
        cls._sql_constraints = [
            ('forecast_monthly_line_unique',
                Unique(table, table.forecast, table.product_template,
                    table.month_start),
                'sales_forecast.msg_forecast_monthly_line_unique'),
        ]
        cls._order.insert(0, ('month_start', 'ASC'))
        cls._order.insert(1, ('product_template', 'ASC'))

    def get_month_end(self, name):
        if not self.month_start:
            return
        if self.month_start.month == 12:
            next_month = self.month_start.replace(
                year=self.month_start.year + 1, month=1, day=1)
        else:
            next_month = self.month_start.replace(
                month=self.month_start.month + 1, day=1)
        return next_month - timedelta(days=1)
