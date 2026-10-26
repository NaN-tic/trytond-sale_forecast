import unittest
from datetime import date, timedelta

from proteus import Model
from trytond.modules.company.tests.tools import create_company
from trytond.tests.test_tryton import drop_db
from trytond.tests.tools import activate_modules


class TestOptionalCategoryIncreases(unittest.TestCase):

    def setUp(self):
        drop_db()
        super().setUp()

    def tearDown(self):
        drop_db()
        super().tearDown()

    def test(self):
        activate_modules('sale_forecast')
        create_company()
        Category = Model.get('product.category')
        Template = Model.get('product.template')
        Uom = Model.get('product.uom')
        Calculation = Model.get('sales.forecast.calculation')
        unit, = Uom.find([('name', '=', 'Unit')])
        for name in ['Bread', 'Pastry']:
            category = Category(name=name)
            category.save()
            template = Template(name=name, default_uom=unit, type='goods',
                salable=True, sale_uom=unit)
            template.categories.append(category)
            template.save()
        calculation = Calculation(1)
        calculation.start_date = date.today()
        calculation.end_date = date.today() + timedelta(days=6)
        calculation.general_increase_percentage = 0.1
        calculation.save()
        calculation.click('load_general_categories')
        calculation.reload()
        self.assertEqual(len(calculation.category_increase_lines), 2)

        calculation.category_increase_lines.remove(
            calculation.category_increase_lines[-1])
        calculation.category_increase_lines[0].percentage = None
        calculation.save()
        calculation.click('calculate_general')
        calculation.reload()
        self.assertEqual(len(calculation.category_increase_lines), 1)
        self.assertIsNone(calculation.category_increase_lines[0].percentage)
        self.assertEqual(calculation.generated_forecasts[0]
            .general_increase_percentage, 0.1)

        calculation.category_increase_lines.remove(
            calculation.category_increase_lines[-1])
        calculation.save()
        calculation.click('calculate_general')
        calculation.reload()
        self.assertFalse(calculation.category_increase_lines)
        forecasts = calculation.generated_forecasts
        self.assertEqual(len(forecasts), 2)
        self.assertEqual(
            [forecast.id for forecast in Model.get('sales.forecast').find([])],
            [forecast.id for forecast in forecasts])
        self.assertEqual(
            [(forecast.computed_at, forecast.id) for forecast in forecasts],
            sorted(((forecast.computed_at, forecast.id)
                    for forecast in forecasts), reverse=True))
        calculation.click('load_general_categories')
        calculation.reload()
        self.assertEqual(len(calculation.category_increase_lines), 2)
