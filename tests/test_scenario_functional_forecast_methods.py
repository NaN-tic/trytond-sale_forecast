import math
import unittest
from datetime import date, timedelta
from unittest.mock import patch

from proteus import Model
from proteus.config import get_config
from trytond.modules.company.tests.tools import create_company, get_company
from trytond.exceptions import UserError
from trytond.tests.test_tryton import drop_db
from trytond.tests.tools import activate_modules


class TestFunctionalForecastMethods(unittest.TestCase):

    def setUp(self):
        drop_db()
        super().setUp()

    def tearDown(self):
        drop_db()
        super().tearDown()

    def test(self):
        activate_modules('sale_forecast')

        Calendar = Model.get('sales.forecast.calendar')
        Calculation = Model.get('sales.forecast.calculation')
        DailyLine = Model.get('sales.forecast.daily_line')
        Forecast = Model.get('sales.forecast')
        ManualLine = Model.get('sales.forecast.manual_line')
        ProductDayLine = Model.get('sales.forecast.calendar.day.product')
        ProductTemplate = Model.get('product.template')
        ProductCategory = Model.get('product.category')
        ProductUom = Model.get('product.uom')

        create_company()
        company = get_company()
        unit, = ProductUom.find([('name', '=', 'Unit')])
        week_start = date.today() - timedelta(days=date.today().weekday())

        def create_template(name, method):
            template = ProductTemplate(
                name=name,
                default_uom=unit,
                sale_uom=unit,
                salable=True,
                type='goods',
                cost_price_method='fixed')
            template.sale_forecast_method = method
            template.sale_forecast_boxes_per_pallet = 20
            if method == 'new_product':
                template.sale_forecast_new_product_source = 'last_month'
                template.sale_forecast_new_product_increase_percentage = 0.2
            package = template.packages.new()
            package.name = 'Box'
            package.quantity = 10
            package.is_default = True
            template.save()
            return template

        general = create_template('General', 'general')
        family = ProductCategory(name='Forecast Family')
        family.save()
        general.categories.append(family)
        general.save()
        manual = create_template('Manual', 'manual')
        key_account = create_template('Key Account', 'key_accounts')
        key_account.sale_forecast_key_account_source = 'customer_forecast'
        key_account.sale_forecast_customer_trucks_per_week = 2
        key_account.sale_forecast_customer_pallets_per_truck = 3
        key_account.save()
        new_product = create_template('New Product', 'new_product')

        calculation = Calculation(1)
        calculation.company = company
        calculation.start_date = week_start
        calculation.end_date = week_start + timedelta(days=6)
        calculation.product_templates.append(ProductTemplate(general.id))
        calculation.save()
        calculation.reload()
        self.assertEqual(
            [line.category.id for line in calculation.category_increase_lines],
            [family.id])

        calculation.product_templates.pop()
        calculation.product_templates.append(ProductTemplate(manual.id))
        calculation.save()
        action = Calculation._proxy.open_manual_lines(
            [calculation.id], get_config().context)
        self.assertEqual(action['views'][0][1], 'form')

        manual_line = ManualLine(
            product_template=manual,
            boxes=5)
        self.assertEqual(manual_line.week_start, week_start)
        manual_line.save()
        self.assertEqual(manual_line.units, 50.0)
        self.assertEqual(manual_line.pallets, 0.25)
        manual.reload()
        self.assertIsNotNone(manual.sale_forecast_manual_updated_at)
        updated_at = manual.sale_forecast_manual_updated_at
        manual.click('duplicate_last_manual_forecast')
        manual_lines = ManualLine.find([
                ('product_template', '=', manual.id),
                ])
        self.assertEqual(len(manual_lines), 2)
        self.assertEqual(manual_lines[1].week_start,
            week_start + timedelta(days=7))
        self.assertEqual(manual_lines[1].units, 50.0)
        manual.reload()
        self.assertGreaterEqual(manual.sale_forecast_manual_updated_at,
            updated_at)
        with self.assertRaises(UserError):
            manual_lines[0].delete()

        calendar = Calendar(name='Functional Calendar')
        product_day = calendar.day_lines.new()
        product_day.date = week_start + timedelta(days=1)
        product_day.day_type = 'product_day'
        product_line = product_day.product_lines.new()
        product_line.product_template = general
        product_line.units = 200
        calendar.save()
        product_line, = ProductDayLine.find([
                ('calendar_day.calendar', '=', calendar.id),
                ('product_template', '=', general.id),
                ])
        product_line.boxes = 30
        product_line.save()
        product_line.reload()
        self.assertEqual(product_line.units, 300.0)
        self.assertEqual(product_line.pallets, 1.5)
        calendar.click('calculate_event_results')

        equal_distribution = {str(index): 1.0 / 7.0 for index in range(7)}
        general_forecast = Forecast(
            name='General with Product Day',
            company=company,
            calendar=calendar,
            calculation_method='general',
            general_increase_percentage=0,
            start_date=week_start,
            end_date=week_start + timedelta(days=6))
        general_forecast.product_templates.append(general)
        general_forecast.save()
        with patch(
                'trytond.modules.sale_forecast.forecast.'
                'SalesForecast._previous_year_week_quantity',
                return_value=70), patch(
                'trytond.modules.sale_forecast.forecast.'
                'SalesForecast._weekday_percentages',
                return_value=equal_distribution):
            general_forecast.click('calculate')
        general_forecast.reload()
        self.assertEqual(len(general_forecast.lines), 1)
        self.assertEqual(general_forecast.lines[0].base_quantity, 70.0)
        self.assertEqual(general_forecast.lines[0].quantity, 360.0)
        product_day_lines = DailyLine.find([
                ('forecast', '=', general_forecast.id),
                ('date', '=', week_start + timedelta(days=1)),
                ])
        self.assertEqual(product_day_lines[0].quantity, 300.0)

        key_forecast = Forecast(
            name='Customer Pallet Forecast',
            company=company,
            calculation_method='key_accounts',
            start_date=week_start,
            end_date=week_start + timedelta(days=6))
        key_forecast.product_templates.append(key_account)
        key_forecast.save()
        key_forecast.click('calculate')
        key_forecast.reload()
        self.assertEqual(key_forecast.lines[0].units, 1200.0)
        self.assertEqual(key_forecast.lines[0].boxes, 120.0)
        self.assertEqual(key_forecast.lines[0].pallets, 6.0)

        new_forecast = Forecast(
            name='New Product Forecast',
            company=company,
            calculation_method='new_product',
            start_date=week_start,
            end_date=week_start + timedelta(days=6))
        new_forecast.product_templates.append(new_product)
        new_forecast.save()
        reference_quantities = iter([0, 100])

        def new_product_history_quantity(
                history_company, history_template, start_date, end_date):
            if (end_date - start_date).days > 20:
                return next(reference_quantities)
            return 0

        with patch(
                'trytond.modules.sale_forecast.forecast.'
                'SalesForecastSaleHistory.quantity_sum',
                side_effect=new_product_history_quantity), patch(
                'trytond.modules.sale_forecast.forecast.'
                'SalesForecast._month_week_quantity',
                return_value=100):
            new_forecast.click('calculate')
        new_forecast.reload()
        self.assertEqual(new_forecast.lines[0].base_quantity, 120.0)

        manual_forecast = Forecast(
            name='Manual Forecast',
            company=company,
            calculation_method='manual',
            start_date=week_start,
            end_date=week_start + timedelta(days=6))
        manual_forecast.product_templates.append(manual)
        manual_forecast.save()
        manual_forecast.click('calculate')
        manual_forecast.reload()
        self.assertEqual(manual_forecast.lines[0].units, 50.0)
        self.assertEqual(manual_forecast.lines[0].calendar_adjustment, 1.0)
        manual_line.reload()
        manual_line.units = 51
        manual_line.save()
        manual_forecast.click('calculate')
        manual_forecast.reload()
        self.assertEqual(manual_forecast.lines[0].units, 51)
        self.assertEqual(manual_forecast.lines[0].boxes, 6)
        self.assertEqual(manual_forecast.lines[0].pallets, 1)
        for model in ['sales.forecast.daily_line', 'sales.forecast.monthly_line']:
            lines = Model.get(model).find([
                ('forecast', '=', manual_forecast.id)])
            self.assertTrue(lines)
            for line in lines:
                self.assertEqual(line.boxes, math.ceil(line.units / 10))
                self.assertEqual(line.pallets, math.ceil(line.units / 200))

        incomplete_manual_forecast = Forecast(
            name='Incomplete Manual Forecast',
            company=company,
            calculation_method='manual',
            start_date=week_start,
            end_date=week_start + timedelta(days=20))
        incomplete_manual_forecast.product_templates.append(
            ProductTemplate(manual.id))
        incomplete_manual_forecast.save()
        with self.assertRaises(UserError):
            incomplete_manual_forecast.click('calculate')

        calculation.reload()
        calculation.product_templates.pop()
        calculation.general_increase_percentage = 0
        calculation.start_date = week_start
        calculation.end_date = week_start + timedelta(days=6)
        calculation.save()
        with patch(
                'trytond.modules.sale_forecast.forecast.'
                'SalesForecastSaleHistory.quantity_sum', return_value=100), patch(
                'trytond.modules.sale_forecast.forecast.'
                'SalesForecast._month_week_quantity', return_value=100):
            calculation.click('calculate_all')
        calculation.reload()
        forecasts = calculation.generated_forecasts
        self.assertEqual(len(forecasts), 4)
        self.assertEqual({forecast.calculation_method for forecast in forecasts},
            {'general', 'key_accounts', 'new_product', 'manual'})
        for forecast in forecasts:
            self.assertEqual(forecast.state, 'calculated')
            self.assertTrue(forecast.lines)
            self.assertTrue(all(line.method == forecast.calculation_method
                    for line in forecast.lines))
            self.assertIsNotNone(getattr(calculation,
                    'last_%s_calculated_at' % forecast.calculation_method))
