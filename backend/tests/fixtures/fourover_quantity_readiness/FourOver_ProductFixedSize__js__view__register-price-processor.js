/**
 * Created by FindCanary, LLC.
 * Project: 4over
 */

define([
    'uiComponent',
    'FourOver_Project/js/model/product-price-processor',
    'FourOver_ProductFixedSize/js/model/price-processor'
], function (Component, productPriceProcessor, fixedSizePriceProcessor) {
    'use strict';

    productPriceProcessor.setProductPriceCalculator(fixedSizePriceProcessor);

    return Component;
});
