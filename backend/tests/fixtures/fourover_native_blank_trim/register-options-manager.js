/**
 * Created by QASource.
 * Project: 4over
 */

define([
    'uiComponent',
    'FourOver_Project/js/model/product-options-processors',
    'FourOver_TrimSize/js/model/product/trim-size-options-manager'
], function (Component, productOptionsProcessors, trimSizeOptionsManager) {
    'use strict';

    productOptionsProcessors.addParser(trimSizeOptionsManager);
    productOptionsProcessors.addApplier(trimSizeOptionsManager);

    return Component;
});
