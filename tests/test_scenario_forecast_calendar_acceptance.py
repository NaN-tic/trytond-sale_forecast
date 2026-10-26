import unittest
from datetime import date, timedelta
from decimal import Decimal

from proteus import Model
from proteus.config import get_config
from trytond.exceptions import UserError
from trytond.model.exceptions import SQLConstraintError
from trytond.pyson import PYSONDecoder
from trytond.modules.company.tests.tools import create_company, get_company
from trytond.tests.test_tryton import drop_db
from trytond.tests.tools import activate_modules


class TestForecastCalendarAcceptance(unittest.TestCase):

    def setUp(self):
        drop_db()
        super().setUp()

    def tearDown(self):
        drop_db()
        super().tearDown()

    def test(self):
        activate_modules('sale_forecast')
        create_company()
        company = get_company()
        config = get_config()
        Calendar = Model.get('sales.forecast.calendar')
        Calculation = Model.get('sales.forecast.calculation')
        Distribution = Model.get('sales.forecast.distribution')
        Forecast = Model.get('sales.forecast')
        History = Model.get('sales.forecast.sale_history')
        Category = Model.get('product.category')
        PartyCategory = Model.get('party.category')
        Party = Model.get('party.party')
        Template = Model.get('product.template')
        Uom = Model.get('product.uom')
        Sale = Model.get('sale.sale')
        ProductDay = Model.get('sales.forecast.calendar.day.product')
        Special = Model.get('sales.forecast.calendar.day.special')
        Monthly = Model.get('sales.forecast.monthly_line')
        Daily = Model.get('sales.forecast.daily_line')
        unit, = Uom.find([('name', '=', 'Unit')])
        warehouse, = Model.get('stock.location').find([('code', '=', 'WH')])
        category = Category(name='Bread')
        category.save()
        segment = PartyCategory(name='Shops')
        segment.save()
        party = Party(name='Calendar Customer')
        party.categories.append(segment)
        party.addresses.new(city='Barcelona')
        party.save()
        template = Template(name='Calendar Product', default_uom=unit,
            sale_uom=unit, salable=True, type='goods', cost_price_method='fixed')
        template.categories.append(category)
        package = template.packages.new(name='Box', quantity=10,
            is_default=True)
        template.save()
        template.reload()
        package, = template.packages
        template.sale_forecast_package = package
        template.save()
        self.assertTrue(package.active)

        # Pick a full week crossing a month boundary in the near future.
        month = (date.today().replace(day=28) + timedelta(days=4)).replace(day=1)
        if month.weekday() == 0:
            month = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
        week = month - timedelta(days=month.weekday())
        history_year = week.isocalendar().year - 1
        history_week = date.fromisocalendar(history_year,
            min(week.isocalendar().week,
                date(history_year, 12, 28).isocalendar().week), 1)

        def create_sale(day, quantity, state='confirmed'):
            sale = Sale(party=party, invoice_party=party, shipment_party=party,
                warehouse=warehouse, company=company,
                currency=company.currency, sale_date=day, shipping_date=day)
            line = sale.lines.new()
            line.product = template.products[0]
            line.quantity = quantity
            line.unit = unit
            line.unit_price = Decimal('1')
            sale.save()
            if state in {'quotation', 'confirmed'}:
                sale.click('quote')
            if state == 'confirmed':
                sale.click('confirm')
            return sale

        for offset in range(7):
            create_sale(history_week + timedelta(days=offset), 10*(offset+1))
        create_sale(history_week, 100, state='draft')
        create_sale(history_week, 200, state='quotation')

        calendar = Calendar(name='Calendar Acceptance')
        product_day = calendar.day_lines.new(date=week + timedelta(days=1),
            day_type='product_day')
        product_day.product_lines.new(product_template=template, units=200)
        calendar.day_lines.new(date=week + timedelta(days=2), day_type='festive')
        special_day = calendar.day_lines.new(date=week + timedelta(days=3),
            day_type='special_sale')
        special_day.special_lines.new(scope='all_products',
            increase_percentage=0.5)
        calendar.save()
        calendar.reload()
        product_day, = [day for day in calendar.day_lines
            if day.day_type == 'product_day']
        special_day, = [day for day in calendar.day_lines
            if day.day_type == 'special_sale']

        calculation = Calculation(1)
        calculation.company = company
        calculation.calendar = calendar
        calculation.start_date = week
        calculation.end_date = week + timedelta(days=6)
        calculation.general_increase_percentage = 0
        calculation.product_templates.append(template)
        calculation.save()
        self.assertIsNone(calculation.last_calculated_at)
        # No prerequisite button is needed; calculate refreshes everything.
        calculation.click('calculate_general')
        calculation.reload()
        self.assertIsNotNone(calculation.last_calculated_at)
        self.assertEqual(calculation.last_calculated_at,
            calculation.last_general_calculated_at)
        self.assertTrue(calculation.generated_forecasts,
            [(record.id, record.calculation.id if record.calculation else None)
                for record in Forecast.find([])])
        forecast = calculation.generated_forecasts[0]
        forecast.reload()
        ForecastLine = Model.get('sales.forecast.line')
        tree_view = ForecastLine._proxy.fields_view_get(
            None, 'tree', None, config.context)
        self.assertIn('company', tree_view['fields'])
        for field_name in ['product_template', 'product']:
            field = tree_view['fields'][field_name]
            context = PYSONDecoder({'company': forecast.lines[0].company.id}
                ).decode(field['context'])
            self.assertEqual(context['company'], company.id)
            with config.set_context(**context):
                linked_template = Template(template.id)
                self.assertTrue(linked_template.sale_forecast_lines)
                self.assertIsNotNone(
                    linked_template.sale_forecast_last_calculated_at)
        expected = [10, 200, 70, 45, 50, 60, 70]
        daily = Daily.find([('forecast', '=', forecast.id)], order=[('date', 'ASC')])
        self.assertEqual([line.quantity for line in daily], expected)
        self.assertEqual(forecast.lines[0].quantity, sum(expected))
        Result = Model.get('sales.forecast.event_result')
        results = Result.find([('date', '=', special_day.date)])
        self.assertTrue(results)
        self.assertTrue(all(line.date == special_day.date for line in results))
        self.assertTrue(all(line.calendar_day == special_day for line in results))
        self.assertFalse(Result.find([('date', '=', week)]))
        self.assertEqual({line.id for line in Result.find([
                    ('date', '>=', special_day.date),
                    ('date', '<=', special_day.date)])},
            {line.id for line in results})
        product_day.reload()
        other_result_ids = [line.id for line in product_day.result_lines]
        self.assertTrue(other_result_ids)
        for _ in range(2):
            special_day.click('calculate_special_sale')
            special_day.reload()
            self.assertEqual(len(special_day.result_lines), 1)
            self.assertEqual(special_day.result_lines[0].units, 45)
            product_day.reload()
            self.assertEqual(
                [line.id for line in product_day.result_lines], other_result_ids)
        monthly = Monthly.find([('forecast', '=', forecast.id)])
        self.assertEqual(len(monthly), 2)
        for line in monthly:
            self.assertEqual(line.quantity, sum(expected[i] for i in range(7)
                    if (week + timedelta(days=i)).month == line.month_start.month))
        distribution = Distribution.find([
                ('company', '=', company.id), ('product_template', '=', template.id)])
        self.assertAlmostEqual(sum(line.percentage for line in distribution), 100)
        template.reload()
        self.assertIsNotNone(template.sale_forecast_last_calculated_at)
        self.assertEqual([line.id for line in template.sale_forecast_lines],
            [line.id for line in forecast.lines])

        # Edits are reflected without separately recalculating event results.
        product_line, = ProductDay.find([('calendar_day', '=', product_day.id)])
        product_line.boxes = 30
        product_line.save()
        special, = Special.find([('calendar_day', '=', special_day.id)])
        special.scope = 'product_category'
        special.product_category = category
        special.save()
        forecast.click('calculate')
        forecast.reload()
        self.assertEqual(forecast.lines[0].quantity, 605)
        special.scope = 'party_category'
        special.party_category = segment
        special.increase_percentage = 1
        special.save()
        forecast.click('calculate')
        forecast.reload()
        self.assertEqual(forecast.lines[0].quantity, 620)

        with self.assertRaises(UserError):
            Special(calendar_day=special_day, scope='all_products',
                increase_percentage=0).save()
        with self.assertRaises(UserError):
            Special(calendar_day=special_day, scope='party_category',
                party_category=segment, increase_percentage=0).save()
        other_segment = PartyCategory(name='Overlapping Segment')
        other_segment.save()
        party.categories.append(other_segment)
        party.save()
        with self.assertRaises(UserError):
            Special(calendar_day=special_day, scope='party_category',
                party_category=other_segment, increase_percentage=0).save()
        with self.assertRaises(SQLConstraintError):
            ProductDay(calendar_day=product_day,
                product_template=template, units=1).save()

        special.scope = 'product_category'
        special.product_category = category
        special.save()
        child_category = Category(name='Bread Child', parent=category)
        child_category.save()
        with self.assertRaises(UserError):
            Special(calendar_day=special_day, scope='product_category',
                product_category=child_category, increase_percentage=0).save()

        # Archived packaging must not be used for a new calculation.
        package.reload()
        package.active = False
        package.save()
        with self.assertRaises(UserError):
            forecast.click('calculate')
        package.active = True
        package.save()

        first_company = company
        company_party = Party(name='Second Forecast Company')
        company_party.save()
        create_company(party=company_party)
        Company = Model.get('company.company')
        company, = Company.find([('id', '!=', first_company.id)])
        user = Model.get('res.user')(config.user)
        user.companies.append(company)
        user.company_filter = 'all'
        user.save()
        with config.set_context(company=company.id):
            create_sale(history_week, 700)
            second = Forecast(name='Other Company Forecast', company=company,
                calendar=Calendar(calendar.id), calculation_method='general',
                general_increase_percentage=0, start_date=week,
                end_date=week + timedelta(days=6))
            second.product_templates.append(Template(template.id))
            second.save()
            second.click('calculate')
            other_history, = History.find([('company', '=', company.id)])
            self.assertEqual(other_history.quantity, 700)
            other_template = Template(template.id)
            self.assertEqual({line.forecast.id
                    for line in other_template.sale_forecast_lines}, {second.id})
        with config.set_context(company=first_company.id):
            history = History.find([('company', '=', first_company.id)])
            self.assertEqual(sum(line.quantity for line in history), 280)
            template = Template(template.id)
            self.assertEqual({line.forecast.id
                    for line in template.sale_forecast_lines}, {forecast.id})

            # Net returns remain in history but cannot become forecast sales.
            company = first_company
            create_sale(history_week, -1000)
            special_day.click('calculate_special_sale')
            special_day.reload()
            result, = [line for line in special_day.result_lines
                if line.company.id == first_company.id]
            self.assertEqual(result.units, 0)
            self.assertEqual(result.boxes, 0)
            history = History.find([('company', '=', first_company.id)])
            self.assertLess(sum(line.quantity for line in history), 0)
            forecast.click('calculate')
            forecast.reload()
            self.assertTrue(all(line.quantity >= 0 for line in forecast.lines))
            daily = Daily.find([('forecast', '=', forecast.id)])
            self.assertTrue(daily)
            self.assertTrue(all(line.quantity >= 0 for line in daily))
