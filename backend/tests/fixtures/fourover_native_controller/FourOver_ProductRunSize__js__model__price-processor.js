/**
 * Created by FindCanary, LLC.
 * Project: 4over
 */

define([
    'underscore',
    'FourOver_Project/js/model/product-package-prices'
], function (_, productPackagePrices) {
    'use strict';

    return function (qty) {
        var packagePrice = _.findWhere(productPackagePrices(), {qty: qty});
        return packagePrice ? packagePrice.price / qty: undefined;
    };
});
