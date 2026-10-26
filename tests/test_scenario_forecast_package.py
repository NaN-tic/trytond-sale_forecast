import unittest

from proteus import Model
from trytond.modules.company.tests.tools import create_company
from trytond.tests.test_tryton import drop_db
from trytond.tests.tools import activate_modules


class TestForecastPackage(unittest.TestCase):

    def setUp(self):
        drop_db()
        super().setUp()

    def tearDown(self):
        drop_db()
        super().tearDown()

    def test(self):
        activate_modules('sale_forecast')
        create_company()
        Template = Model.get('product.template')
        Uom = Model.get('product.uom')
        unit, = Uom.find([('name', '=', 'Unit')])
        template = Template(name='Forecast Packaging', default_uom=unit,
            type='goods')
        template.packages.new(name='Box', quantity=30, is_default=True)
        template.packages.new(name='Small Box', quantity=15, is_default=False)
        template.packages.new(name='Manual Pallet', quantity=10,
            is_default=False)
        template.save()
        packages = {package.name: package for package in template.packages}

        template.sale_forecast_boxes_per_pallet = 20
        template.sale_forecast_package = packages['Box']
        self.assertEqual(template.sale_forecast_boxes_per_pallet, 20)
        template.save()
        template.reload()
        self.assertEqual(template.sale_forecast_boxes_per_pallet, 20)

        template.sale_forecast_package = packages['Small Box']
        self.assertEqual(template.sale_forecast_boxes_per_pallet, 20)
        template.sale_forecast_boxes_per_pallet = 24
        template.sale_forecast_package = packages['Manual Pallet']
        self.assertEqual(template.sale_forecast_boxes_per_pallet, 24)
        template.sale_forecast_package = None
        self.assertEqual(template.sale_forecast_boxes_per_pallet, 24)
        template.save()
        template.reload()
        self.assertEqual(template.sale_forecast_boxes_per_pallet, 24)
