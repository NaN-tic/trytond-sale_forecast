# This file is part of Tryton.  The COPYRIGHT file at the top level of
# this repository contains the full copyright notices and license terms.
from datetime import timedelta

from dateutil.relativedelta import relativedelta
from sql.aggregate import Sum

from trytond.exceptions import UserError
from trytond.i18n import gettext
from trytond.model import ModelView, fields
from trytond.pool import Pool, PoolMeta
from trytond.pyson import Eval, If
from trytond.tools import grouped_slice
from trytond.transaction import Transaction

FORECAST_METHODS = [
    ('general', 'General Method'),
    ('key_accounts', 'Key Accounts'),
    ('new_product', 'New Product'),
    ('manual', 'Manual'),
]
KEY_ACCOUNT_SOURCES = [
    ('orders', 'Orders in Tryton'),
    ('customer_forecast', 'Customer Forecast'),
    ('mixed', 'Mixed'),
]
NEW_PRODUCT_SOURCES = [
    ('average_months', 'Average Months with History'),
    ('last_month', 'Last Closed Month'),
    ('reference_product', 'Reference Product'),
]


class Package(metaclass=PoolMeta):
    __name__ = 'product.package'

    active = fields.Boolean('Active')

    @staticmethod
    def default_active():
        return True


class Product(metaclass=PoolMeta):
    __name__ = 'product.product'

    @classmethod
    def __setup__(cls):
        super().__setup__()
        # TemplateFunction does not preserve the setter of Function fields.
        cls.sale_forecast_lines.setter = '_set_template_function'
        cls._buttons.update({
                'duplicate_last_manual_forecast': {
                    'icon': 'tryton-copy',
                    'invisible': Eval('sale_forecast_method') != 'manual',
                    'depends': ['sale_forecast_method'],
                    },
                })

    @classmethod
    @ModelView.button
    def duplicate_last_manual_forecast(cls, products):
        Template = Pool().get('product.template')
        templates = list({p.template.id: p.template for p in products}.values())
        Template.duplicate_last_manual_forecast(templates)


class Template(metaclass=PoolMeta):
    __name__ = 'product.template'

    sale_forecast_method = fields.Selection(
        FORECAST_METHODS, 'Forecast Method', required=True,
        help='General: use the corresponding week last year and apply the '
            'product, category or general increase. Key accounts: use '
            'orders or customer volumes. New product: extrapolate '
            'historical months or a reference product. Manual: use the '
            'quantities entered for each week.')
    sale_forecast_package = fields.Many2One(
        'product.package', 'Forecast Package', ondelete='RESTRICT',
        domain=[
            ('template', '=', Eval('id', -1)),
            ('active', '=', True),
        ],
        help='Package used to display forecast quantities in boxes and '
            'pallets. If empty, the default product package is used.')
    sale_forecast_boxes_per_pallet = fields.Integer(
        'Forecast Boxes per Pallet',
        help='Fallback used when the selected package does not define boxes '
            'per pallet.')
    sale_forecast_new_product_source = fields.Selection(
        NEW_PRODUCT_SOURCES, 'New Product Forecast Source',
        states={
            'invisible': Eval('sale_forecast_method') != 'new_product',
            'required': Eval('sale_forecast_method') == 'new_product',
            },
        help='Average months: average corresponding weeks in closed months '
            'with sales history. Last closed month: use the most recent '
            'closed month with history. Reference product: use its '
            'historical months and weekday distribution, even when this '
            'product has no sales. The new-product increase is applied '
            'afterwards.')
    sale_forecast_reference_product = fields.Many2One(
        'product.template', 'Reference Product', ondelete='RESTRICT',
        domain=[
            ('id', '!=', Eval('id', -1)),
            ('active', '=', True),
            ('salable', '=', True),
            ('type', '=', 'goods'),
            ('default_uom.category', '=', Eval('default_uom_category', -1)),
            ],
        context={'company': Eval('context', {}).get('company', -1)},
        states={
            'invisible': (Eval('sale_forecast_method') != 'new_product')
                | (Eval('sale_forecast_new_product_source') != 'reference_product'),
            'required': (Eval('sale_forecast_method') == 'new_product')
                & (Eval('sale_forecast_new_product_source') == 'reference_product'),
            },
        help='Use this product sales history and weekday distribution. '
            'Average closed months with history from the reference product.')
    sale_forecast_new_product_increase_percentage = fields.Float(
        'New Product Increase %',
        states={
            'invisible': Eval('sale_forecast_method') != 'new_product',
            'required': Eval('sale_forecast_method') == 'new_product',
            },
        help='Enter a percentage, for example 20 for 20%%.')
    sale_forecast_key_account_source = fields.Selection(
        KEY_ACCOUNT_SOURCES, 'Key Account Source',
        states={
            'invisible': Eval('sale_forecast_method') != 'key_accounts',
            'required': Eval('sale_forecast_method') == 'key_accounts',
            },
        help='Orders: positive quantities from all non-cancelled sales, '
            'using shipping date or sale date when shipping date is '
            'missing. Customer forecast: trucks per week multiplied by '
            'pallets per truck and package capacity. Mixed: use orders when '
            'present; otherwise fall back to the general method.')
    sale_forecast_customer_trucks_per_week = fields.Float(
        'Customer Trucks per Week',
        domain=[
            If((Eval('sale_forecast_method') == 'key_accounts')
                & (Eval('sale_forecast_key_account_source') ==
                    'customer_forecast'),
                ('sale_forecast_customer_trucks_per_week', '>', 0),
                ()),
        ],
        states={
            'invisible': (Eval('sale_forecast_method') != 'key_accounts')
                | (Eval('sale_forecast_key_account_source') !=
                    'customer_forecast'),
            'required': (Eval('sale_forecast_method') == 'key_accounts')
                & (Eval('sale_forecast_key_account_source') ==
                    'customer_forecast'),
            })
    sale_forecast_customer_pallets_per_truck = fields.Float(
        'Customer Pallets per Truck',
        domain=[
            If((Eval('sale_forecast_method') == 'key_accounts')
                & (Eval('sale_forecast_key_account_source') ==
                    'customer_forecast'),
                ('sale_forecast_customer_pallets_per_truck', '>', 0),
                ()),
        ],
        states={
            'invisible': (Eval('sale_forecast_method') != 'key_accounts')
                | (Eval('sale_forecast_key_account_source') !=
                    'customer_forecast'),
            'required': (Eval('sale_forecast_method') == 'key_accounts')
                & (Eval('sale_forecast_key_account_source') ==
                    'customer_forecast'),
            })
    sale_forecast_distribution_lines = fields.One2Many(
        'sales.forecast.distribution', 'product_template',
        'Sale Distribution by Day', readonly=True,
        domain=[
            ('company', '=', Eval('context', {}).get('company', -1)),
        ], order=[
            ('sequence', 'ASC'),
            ])
    sale_forecast_manual_lines = fields.One2Many(
        'sales.forecast.manual_line', 'product_template',
        'Manual Weekly Forecasts',
        states={
            'invisible': Eval('sale_forecast_method') != 'manual',
            }, context={'default_product_template': Eval('id', -1)},
        help='Enter the forecast quantity for each Monday-based week. Every '
            'week in the requested period must have a line. Units, boxes '
            'and pallets are converted using the product packaging. Manual '
            'quantities are not adjusted by calendar events.')
    sale_forecast_manual_updated_at = fields.DateTime(
        'Last Manual Forecast Update', readonly=True,
        states={'invisible': Eval('sale_forecast_method') != 'manual'})
    sale_forecast_lines = fields.Function(fields.One2Many(
            'sales.forecast.line', None, 'Latest Sales Forecast',
            readonly=True),
        'get_latest_sale_forecast_lines',
        setter='set_latest_sale_forecast_lines')
    sale_forecast_last_calculated_at = fields.Function(
        fields.DateTime('Last Sales Forecast Calculation', readonly=True),
        'get_latest_sale_forecast_date')

    sale_forecast_previous_year_growth = fields.Function(fields.Float(
            'Year-to-Date Growth %', readonly=True,
            help='Change in units sold this year through yesterday compared with '
            'the same period last year. Empty when the comparison quantity '
            'is zero or negative.'), 'get_sale_history_growth')
    sale_forecast_previous_month_growth = fields.Function(fields.Float(
            'Previous Month Year-over-Year Growth %', readonly=True,
            help='Change in units sold in the previous closed month compared '
            'with the same month last year. Empty when the comparison '
            'quantity is zero or negative.'), 'get_sale_history_growth')

    @classmethod
    def get_sale_history_growth(cls, templates, names):
        pool = Pool()
        history = pool.get('sales.forecast.sale_history').__table__()
        company = Transaction().context.get('company')
        result = {name: {t.id: None for t in templates} for name in names}
        if not company:
            return result
        today = pool.get('ir.date').today()
        month_start = today.replace(day=1)
        year_start = today.replace(month=1, day=1)
        periods = {
            'sale_forecast_previous_year_growth': (
                year_start - relativedelta(years=1),
                (today - timedelta(days=1) - relativedelta(years=1)
                    + timedelta(days=1)), year_start, today),
            'sale_forecast_previous_month_growth': (
                month_start - relativedelta(years=1, months=1),
                month_start - relativedelta(years=1),
                month_start - relativedelta(months=1), month_start),
            }
        cursor = Transaction().connection.cursor()
        for name in names:
            start, previous_end, current_start, end = periods[name]
            if current_start >= end:
                continue
            for batch in grouped_slice(templates):
                ids = [t.id for t in batch]
                totals = []
                for lower, upper in ((start, previous_end), (current_start, end)):
                    cursor.execute(*history.select(
                            history.product_template, Sum(history.quantity),
                            where=(history.company == company)
                                & history.product_template.in_(ids)
                                & (history.date >= lower)
                                & (history.date < upper),
                            group_by=history.product_template))
                    totals.append(dict(cursor.fetchall()))
                previous, current = totals
                for template_id in ids:
                    base = previous.get(template_id, 0)
                    if base > 0:
                        result[name][template_id] = (
                            current.get(template_id, 0) - base) / base
        return result

    @classmethod
    def __setup__(cls):
        super().__setup__()
        cls._buttons.update({
                'duplicate_last_manual_forecast': {
                    'icon': 'tryton-copy',
                    'invisible': Eval('sale_forecast_method') != 'manual',
                    'depends': ['sale_forecast_method'],
                    },
                })

    @staticmethod
    def default_sale_forecast_method():
        return 'general'

    @staticmethod
    def default_sale_forecast_key_account_source():
        return 'orders'

    @staticmethod
    def default_sale_forecast_new_product_source():
        return 'average_months'

    @fields.depends('sale_forecast_package')
    def on_change_sale_forecast_package(self):
        boxes_per_pallet = getattr(
            self.sale_forecast_package, 'box_per_pallet', None)
        if boxes_per_pallet:
            self.sale_forecast_boxes_per_pallet = boxes_per_pallet

    @classmethod
    def get_latest_sale_forecast_lines(cls, templates, name):
        Line = Pool().get('sales.forecast.line')
        result = {}
        for template in templates:
            lines = Line.search([
                    ('product_template', '=', template.id),
                    ('forecast.state', '=', 'calculated'),
                    ('forecast.company', '=',
                        Transaction().context.get('company')),
                    ], order=[
                    ('forecast.computed_at', 'DESC'),
                    ('forecast.id', 'DESC'),
                    ('week_start', 'ASC'),
                    ])
            latest = {}
            for line in lines:
                if (line.forecast.start_date <= line.week_start
                        and line.forecast.end_date >= line.week_end):
                    latest.setdefault(line.week_start, line.id)
            result[template.id] = [latest[week] for week in sorted(latest)]
        return result

    @classmethod
    def get_latest_sale_forecast_date(cls, templates, name):
        Line = Pool().get('sales.forecast.line')
        latest = cls.get_latest_sale_forecast_lines(
            templates, 'sale_forecast_lines')
        return {template.id: max(
                (line.forecast.computed_at
                    for line in Line.browse(latest[template.id])), default=None)
            for template in templates}

    @classmethod
    def set_latest_sale_forecast_lines(cls, templates, name, value):
        pass

    @classmethod
    @ModelView.button
    def duplicate_last_manual_forecast(cls, templates):
        ManualLine = Pool().get('sales.forecast.manual_line')
        for template in templates:
            lines = ManualLine.search([
                    ('product_template', '=', template.id),
                    ], order=[('week_start', 'DESC')], limit=1)
            if not lines:
                raise UserError(gettext(
                    'sale_forecast.msg_missing_manual_line',
                    product=template.rec_name))
            previous = lines[0]
            ManualLine.create([{
                        'product_template': template.id,
                        'week_start': previous.week_start + timedelta(days=7),
                        'units': previous.units,
                        }])
