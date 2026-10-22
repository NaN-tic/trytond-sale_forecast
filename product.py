# This file is part of Tryton.  The COPYRIGHT file at the top level of
# this repository contains the full copyright notices and license terms.
from datetime import timedelta

from trytond.exceptions import UserError
from trytond.i18n import gettext
from trytond.model import ModelView, fields
from trytond.pool import Pool, PoolMeta
from trytond.pyson import Eval, If
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
]


class Package(metaclass=PoolMeta):
    __name__ = 'product.package'

    active = fields.Boolean('Active')

    @staticmethod
    def default_active():
        return True


class Template(metaclass=PoolMeta):
    __name__ = 'product.template'

    sales_forecast_method = fields.Selection(
        FORECAST_METHODS, 'Forecast Method', required=True)
    sales_forecast_package = fields.Many2One(
        'product.package', 'Forecast Package', ondelete='RESTRICT',
        domain=[
            ('template', '=', Eval('id', -1)),
            ('active', '=', True),
        ],
        help='Package used to display forecast quantities in boxes and '
            'pallets. If empty, the default product package is used.')
    sales_forecast_boxes_per_pallet = fields.Integer(
        'Forecast Boxes per Pallet',
        help='Fallback used when the selected package does not define boxes '
            'per pallet.')
    sales_forecast_new_product_source = fields.Selection(
        NEW_PRODUCT_SOURCES, 'New Product Forecast Source',
        states={
            'invisible': Eval('sales_forecast_method') != 'new_product',
            'required': Eval('sales_forecast_method') == 'new_product',
            })
    sales_forecast_new_product_increase_percentage = fields.Float(
        'New Product Increase %',
        states={
            'invisible': Eval('sales_forecast_method') != 'new_product',
            'required': Eval('sales_forecast_method') == 'new_product',
            },
        help='Enter a percentage, for example 20 for 20%%.')
    sales_forecast_key_account_source = fields.Selection(
        KEY_ACCOUNT_SOURCES, 'Key Account Source',
        states={
            'invisible': Eval('sales_forecast_method') != 'key_accounts',
            'required': Eval('sales_forecast_method') == 'key_accounts',
            })
    sales_forecast_customer_trucks_per_week = fields.Float(
        'Customer Trucks per Week',
        domain=[
            If((Eval('sales_forecast_method') == 'key_accounts')
                & (Eval('sales_forecast_key_account_source') ==
                    'customer_forecast'),
                ('sales_forecast_customer_trucks_per_week', '>', 0),
                ()),
        ],
        states={
            'invisible': (Eval('sales_forecast_method') != 'key_accounts')
                | (Eval('sales_forecast_key_account_source') !=
                    'customer_forecast'),
            'required': (Eval('sales_forecast_method') == 'key_accounts')
                & (Eval('sales_forecast_key_account_source') ==
                    'customer_forecast'),
            })
    sales_forecast_customer_pallets_per_truck = fields.Float(
        'Customer Pallets per Truck',
        domain=[
            If((Eval('sales_forecast_method') == 'key_accounts')
                & (Eval('sales_forecast_key_account_source') ==
                    'customer_forecast'),
                ('sales_forecast_customer_pallets_per_truck', '>', 0),
                ()),
        ],
        states={
            'invisible': (Eval('sales_forecast_method') != 'key_accounts')
                | (Eval('sales_forecast_key_account_source') !=
                    'customer_forecast'),
            'required': (Eval('sales_forecast_method') == 'key_accounts')
                & (Eval('sales_forecast_key_account_source') ==
                    'customer_forecast'),
            })
    sales_forecast_distribution_lines = fields.One2Many(
        'sales.forecast.distribution', 'product_template',
        'Sale Distribution by Day', readonly=True,
        domain=[
            ('company', '=', Eval('context', {}).get('company', -1)),
        ], order=[
            ('sequence', 'ASC'),
            ])
    sales_forecast_manual_lines = fields.One2Many(
        'sales.forecast.manual_line', 'product_template',
        'Manual Weekly Forecasts',
        states={
            'invisible': Eval('sales_forecast_method') != 'manual',
            }, context={'default_product_template': Eval('id', -1)})
    sales_forecast_manual_updated_at = fields.DateTime(
        'Last Manual Forecast Update', readonly=True,
        states={'invisible': Eval('sales_forecast_method') != 'manual'})
    sales_forecast_lines = fields.Function(fields.One2Many(
            'sales.forecast.line', None, 'Latest Sales Forecast',
            readonly=True),
        'get_latest_sales_forecast_lines',
        setter='set_latest_sales_forecast_lines')
    sales_forecast_last_calculated_at = fields.Function(
        fields.DateTime('Last Sales Forecast Calculation', readonly=True),
        'get_latest_sales_forecast_date')

    @classmethod
    def __setup__(cls):
        super().__setup__()
        cls._buttons.update({
                'duplicate_last_manual_forecast': {
                    'icon': 'tryton-copy',
                    'invisible': Eval('sales_forecast_method') != 'manual',
                    'depends': ['sales_forecast_method'],
                    },
                })

    @staticmethod
    def default_sales_forecast_method():
        return 'general'

    @staticmethod
    def default_sales_forecast_key_account_source():
        return 'orders'

    @staticmethod
    def default_sales_forecast_new_product_source():
        return 'average_months'

    @fields.depends('sales_forecast_package')
    def on_change_sales_forecast_package(self):
        boxes_per_pallet = getattr(
            self.sales_forecast_package, 'box_per_pallet', None)
        if boxes_per_pallet:
            self.sales_forecast_boxes_per_pallet = boxes_per_pallet

    @classmethod
    def get_latest_sales_forecast_lines(cls, templates, name):
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
    def get_latest_sales_forecast_date(cls, templates, name):
        Line = Pool().get('sales.forecast.line')
        latest = cls.get_latest_sales_forecast_lines(
            templates, 'sales_forecast_lines')
        return {template.id: max(
                (line.forecast.computed_at
                    for line in Line.browse(latest[template.id])), default=None)
            for template in templates}

    @classmethod
    def set_latest_sales_forecast_lines(cls, templates, name, value):
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
                    'sales_forecast.msg_missing_manual_line',
                    product=template.rec_name))
            previous = lines[0]
            ManualLine.create([{
                        'product_template': template.id,
                        'week_start': previous.week_start + timedelta(days=7),
                        'units': previous.units,
                        }])
