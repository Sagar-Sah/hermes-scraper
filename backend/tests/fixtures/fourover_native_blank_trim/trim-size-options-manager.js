/**
 * Created by QASource.
 * Project: 4over
 */

define([
    'FourOver_TrimSize/js/model/product/trim-size',
    'FourOver_TrimSize/js/model/custom-trim-size',
    'FourOver_TrimSize/js/model/custom-trim-size-select'
], function (trimSize, customTrimSize, customTrimSizeSelect) {
    'use strict';

    return {
        parse: function (productOptions) {
            productOptions['trim_size'] = customTrimSize() ? trimSize() : '';
            return productOptions;
        },

        apply: function (productOptions) {
            trimSize(productOptions['trim_size']);
            if (trimSize()) {
                let sizeStr = trimSize().replace(/"/g, '');
                let parts = sizeStr.split('x');
                if (parts.length === 2) {
                    let widthVal = parseFloat(parts[0].trim());
                    let heightVal = parseFloat(parts[1].trim());

                    let widthInteger = Math.floor(widthVal);
                    let widthDecimal = parseFloat((widthVal - widthInteger).toFixed(3));

                    let heightInteger = Math.floor(heightVal);
                    let heightDecimal = parseFloat((heightVal - heightInteger).toFixed(3));

                    customTrimSizeSelect().trimSizeWidth(widthInteger);
                    customTrimSizeSelect().trimSizeWidthDecimal(widthDecimal);
                    customTrimSizeSelect().trimSizeHeight(heightInteger);
                    customTrimSizeSelect().trimSizeHeightDecimal(heightDecimal);
                    customTrimSize(true);
                }
            }
        }
    };
});
