# This file is part of Tryton.  The COPYRIGHT file at the top level of
# this repository contains the full copyright notices and license terms.
from datetime import date, timedelta

from trytond.modules.company.tests import CompanyTestMixin
from trytond.pool import Pool
from trytond.tests.test_tryton import ModuleTestCase, with_transaction


class SalesForecastTestCase(ModuleTestCase, CompanyTestMixin):
    'Test Sales Forecast module'
    module = 'sale_forecast'

    @with_transaction()
    def test_duplicate_last_manual_forecast_from_variants(self):
        "Variants of the same template duplicate its forecast only once."
        pool = Pool()
        Product = pool.get('product.product')
        Template = pool.get('product.template')
        Uom = pool.get('product.uom')
        ManualLine = pool.get('sales.forecast.manual_line')
        unit, = Uom.search([('name', '=', 'Unit')])
        template, = Template.create([{
                    'name': 'Manual forecast',
                    'type': 'goods',
                    'default_uom': unit.id,
                    'sale_uom': unit.id,
                    'salable': True,
                    'sale_forecast_method': 'manual',
                    }])
        products = Product.create([{'template': template.id}] * 2)
        week = date(2026, 10, 26)
        ManualLine.create([{
                    'product_template': template.id,
                    'week_start': week,
                    'units': 50,
                    }])

        Product.duplicate_last_manual_forecast(products)

        lines = ManualLine.search([
                ('product_template', '=', template.id),
                ], order=[('week_start', 'ASC')])
        self.assertEqual(
            [(line.week_start, line.units) for line in lines],
            [(week, 50), (week + timedelta(days=7), 50)])


del ModuleTestCase
