/**
 * Created by FindCanary, LLC.
 * Project: 4over
 */

define([
    'uiComponent',
    'FourOver_Project/js/model/product-price-processor',
    'FourOver_ProductRunSize/js/model/price-processor'
], function (Component, productPriceProcessor, packagePriceProcessor) {
    'use strict';

    productPriceProcessor.setProductPriceCalculator(packagePriceProcessor);

    return Component;
});
