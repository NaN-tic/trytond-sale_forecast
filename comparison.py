# This file is part of Tryton. The COPYRIGHT file contains the license terms.
from collections import defaultdict
from datetime import datetime, timedelta

from trytond.exceptions import UserError
from trytond.i18n import gettext
from trytond.model import ModelSQL, ModelView, fields
from trytond.pool import Pool
from trytond.pyson import Eval
from trytond.transaction import Transaction

from .forecast import _convert_packaging_quantities
from .product import FORECAST_METHODS


class ComparisonProduct(ModelSQL):
    __name__ = 'sales.forecast.comparison-product.template'
    comparison = fields.Many2One('sales.forecast.comparison', 'Comparison',
        required=True, ondelete='CASCADE')
    product = fields.Many2One('product.template', 'Product',
        required=True, ondelete='CASCADE')


class ComparisonCategory(ModelSQL):
    __name__ = 'sales.forecast.comparison-product.category'
    comparison = fields.Many2One('sales.forecast.comparison', 'Comparison',
        required=True, ondelete='CASCADE')
    category = fields.Many2One('product.category', 'Category',
        required=True, ondelete='CASCADE')


class Comparison(ModelSQL, ModelView):
    'Sales Forecast Compared with Actual Sales'
    __name__ = 'sales.forecast.comparison'
    _rec_name = 'name'

    name = fields.Char('Name', required=True)
    company = fields.Many2One('company.company', 'Company', required=True)
    start_date = fields.Date('First Week', required=True,
        help='Select any date in the first week to include.')
    end_date = fields.Date('Last Week', required=True,
        help='Select any date in the last week to include.')
    weekly = fields.Boolean('Weekly Detail')
    products = fields.Many2Many('sales.forecast.comparison-product.template',
        'comparison', 'product', 'Products',
        domain=[('salable', '=', True), ('type', '=', 'goods')],
        context={'company': Eval('company', -1)})
    categories = fields.Many2Many('sales.forecast.comparison-product.category',
        'comparison', 'category', 'Product Families',
        context={'company': Eval('company', -1)})
    method = fields.Selection([('', 'All Methods')] + FORECAST_METHODS,
        'Forecast Method')
    unit = fields.Selection([
            ('units', 'Units'), ('boxes', 'Boxes'), ('pallets', 'Pallets'),
            ], 'Display Unit', required=True)
    calculated_at = fields.DateTime('Calculated At', readonly=True)
    lines = fields.One2Many('sales.forecast.comparison.line', 'comparison',
        'Comparison', readonly=True)

    @staticmethod
    def default_company():
        return Transaction().context.get('company')

    @staticmethod
    def default_weekly():
        return True

    @staticmethod
    def default_unit():
        return 'units'

    @classmethod
    def __setup__(cls):
        super().__setup__()
        cls._buttons.update({'calculate': {'icon': 'tryton-refresh'}})

    @classmethod
    def write(cls, *args):
        actions = iter(args)
        updated = []
        for reports, values in zip(actions, actions):
            if values.keys() & {'company', 'start_date', 'end_date', 'weekly',
                    'products', 'categories', 'method', 'unit'}:
                Line = Pool().get('sales.forecast.comparison.line')
                with Transaction().set_context(_check_access=False):
                    Line.delete([line for report in reports
                            for line in report.lines])
                values = {**values, 'calculated_at': None}
            updated.extend((reports, values))
        super().write(*updated)

    @classmethod
    @ModelView.button
    def calculate(cls, records):
        pool = Pool()
        Template = pool.get('product.template')
        Forecast = pool.get('sales.forecast')
        ForecastLine = pool.get('sales.forecast.line')
        History = pool.get('sales.forecast.sale_history')
        Line = pool.get('sales.forecast.comparison.line')
        for report in records:
            if report.start_date > report.end_date:
                raise UserError(gettext('sale_forecast.msg_invalid_period'))
            start = report.start_date - timedelta(
                days=report.start_date.weekday())
            end = report.end_date + timedelta(days=6-report.end_date.weekday())
            domain = [('salable', '=', True), ('type', '=', 'goods')]
            if report.method:
                domain.append(('sale_forecast_method', '=', report.method))
            if report.categories:
                domain.append(('categories_all', 'child_of',
                    [c.id for c in report.categories], 'parent'))
            if report.products:
                domain.append(('id', 'in', [p.id for p in report.products]))
            with Transaction().set_context(active_test=False):
                templates = Template.search(domain, order=[('name', 'ASC')])
            if ((report.products and {p.id for p in report.products}
                        != {p.id for p in templates}) or not templates):
                raise UserError(gettext(
                    'sale_forecast.msg_comparison_selection'))
            weeks = list(Forecast._iter_weeks(start, end))
            latest = {}
            # A forecast of only part of a week is not a complete weekly value.
            for line in ForecastLine.search([
                    ('forecast.company', '=', report.company.id),
                    ('forecast.state', '=', 'calculated'),
                    ('product_template', 'in', [p.id for p in templates]),
                    ('week_start', '>=', start), ('week_start', '<=', end),
                    ], order=[('forecast.computed_at', 'DESC'),
                        ('forecast.id', 'DESC')]):
                if (line.forecast.start_date <= line.week_start
                        and line.forecast.end_date >= line.week_end):
                    latest.setdefault((line.product_template.id,
                            line.week_start), line)
            actual = defaultdict(float)
            for template_id, sale_date, quantity in History._aggregated_sale_rows(
                    report.company, start_date=start, end_date=end):
                week = sale_date - timedelta(days=sale_date.weekday())
                actual[template_id, week] += quantity
            values = []
            subtotals = defaultdict(list)
            for template in templates:
                weekly_values = []
                for week in weeks:
                    line = latest.get((template.id, week))
                    forecast = None
                    if line:
                        forecast = line.units
                    actual_quantity = actual[template.id, week]
                    if report.unit != 'units':
                        if line:
                            forecast = _convert_packaging_quantities(
                                template, units=line.units)[report.unit]
                        actual_quantity = _convert_packaging_quantities(
                            template, units=actual_quantity)[report.unit]
                    weekly_values.append((week, forecast, actual_quantity))
                groups = ([[v] for v in weekly_values] if report.weekly
                    else [weekly_values])
                for group in groups:
                    forecast = (sum(v[1] for v in group)
                        if all(v[1] is not None for v in group) else None)
                    actual_quantity = (sum(v[2] for v in group)
                        if all(v[2] is not None for v in group) else None)
                    week = group[0][0] if report.weekly else None
                    value = {
                        'comparison': report.id,
                        'product': template.id,
                        'method': template.sale_forecast_method,
                        'week_start': week,
                        'forecast_quantity': forecast,
                        'actual_quantity': actual_quantity,
                        'kind': 'product',
                        }
                    values.append(value)
                    subtotals[week].append(value)
            for week, rows in subtotals.items():
                total = {'comparison': report.id, 'week_start': week,
                    'kind': 'subtotal' if report.weekly else 'total'}
                for field in ('forecast_quantity', 'actual_quantity'):
                    total[field] = (sum(row[field] for row in rows)
                        if all(row[field] is not None for row in rows) else None)
                values.append(total)
            if report.weekly:
                total = {'comparison': report.id, 'kind': 'total'}
                rows = [row for row in values if row['kind'] == 'product']
                for field in ('forecast_quantity', 'actual_quantity'):
                    total[field] = (sum(row[field] for row in rows)
                        if all(row[field] is not None for row in rows) else None)
                values.append(total)
            if report.lines:
                Line.delete(report.lines)
            if values:
                Line.create(values)
            cls.write([report], {'calculated_at': datetime.now()})


class ComparisonLine(ModelSQL, ModelView):
    'Sales Forecast Comparison Line'
    __name__ = 'sales.forecast.comparison.line'

    comparison = fields.Many2One('sales.forecast.comparison', 'Comparison',
        required=True, ondelete='CASCADE')
    product = fields.Many2One('product.template', 'Product', readonly=True)
    method = fields.Selection([(None, '')] + FORECAST_METHODS,
        'Forecast Method', readonly=True)
    kind = fields.Selection([
        ('product', 'Product'), ('subtotal', 'Subtotal'), ('total', 'Total'),
        ], 'Row', required=True, readonly=True)
    week_start = fields.Date('Week Start', readonly=True)
    week = fields.Function(fields.Char('Week'), 'get_values')
    unit = fields.Function(fields.Selection([
        ('units', 'Units'), ('boxes', 'Boxes'), ('pallets', 'Pallets')],
        'Display Unit'), 'get_values')
    forecast_quantity = fields.Float('Forecast Quantity', readonly=True)
    actual_quantity = fields.Float('Actual Quantity', readonly=True)
    forecast_display = fields.Function(fields.Char('Forecast'), 'get_values')
    actual_display = fields.Function(fields.Char('Actual Sales'), 'get_values')
    difference = fields.Function(fields.Char('Difference'), 'get_values')
    percentage = fields.Function(fields.Char('Difference %'), 'get_values')

    @classmethod
    def __setup__(cls):
        super().__setup__()
        cls._order = [('week_start', 'ASC NULLS LAST'), ('kind', 'ASC'),
            ('product', 'ASC'), ('id', 'ASC')]

    def get_values(self, name):
        if name == 'week':
            if self.week_start:
                year, week, _ = self.week_start.isocalendar()
                return f'{week:02d}/{year % 100:02d}'
            return ''
        if name == 'unit':
            return self.comparison.unit
        forecast, actual = self.forecast_quantity, self.actual_quantity
        if name == 'forecast_display':
            return f'{forecast:.2f}' if forecast is not None else 'N/A'
        if name == 'actual_display':
            return f'{actual:.2f}' if actual is not None else 'N/A'
        if forecast is None or actual is None:
            return 'N/A'
        if name == 'difference':
            return f'{forecast - actual:.2f}'
        if not actual:
            return 'N/A'
        return f'{(forecast - actual) / actual * 100:.2f}%'
