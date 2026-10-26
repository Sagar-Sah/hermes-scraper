/**
 * Created by FindCanary, LLC.
 * Project: 4over
 */

define([
    'underscore',
    'jquery',
    'FourOver_Project/js/model/product-options-processors',
    'FourOver_Project/js/lib/jquery.serializejson'
], function (_, $, productOptionsProcessors) {
    'use strict';

    var applySuperAttribute = function (optionValues) {
        if ($('.swatch-opt').length) {
            _.forEach(optionValues, function (superAttributeValueId, superAttributeId) {
                var elem = $('[name="super_attribute[' + superAttributeId + ']"]').prev().find('[data-option-id="' + superAttributeValueId + '"]'),
                    parentInput = elem.parent();

                if (elem.hasClass('selected')) {
                    return;
                }

                if (parentInput.hasClass('swatch-select')) {
                    parentInput.val(superAttributeValueId);
                    parentInput.trigger('change');
                } else {
                    elem.trigger('click');
                }
            });
        } else {
            _.forEach($('.field.configurable'), function (field) {
                var parentInput = $(field).find('.super-attribute-select'),
                    attributeId = parentInput.data('attribute-id');

                if (_.has(optionValues, attributeId)) {
                    var elem = $(field).find('[value="' + optionValues[attributeId] + '"]');

                    if (elem?.is(':selected')) {
                        return;
                    }

                    if (elem?.length) {
                        parentInput.val(optionValues[attributeId]);
                    }
                    parentInput.trigger('change');
                }
            });
        }
    }

    var applyOptions = function (optionValues) {
        _.forEach(optionValues, function (optionValueId, optionId) {
            var customOption = $('.product-custom-option[name^="options[' + optionId + ']"]');

            if (customOption.is('.datetime-picker')) {
                _.forEach(optionValueId, function (value, key) {
                    $('.product-custom-option[name="options[' + optionId + '][' + key + ']"]')
                        .val(value)
                        .trigger('change');
                });
            } else {
                customOption.val(optionValueId).trigger('change');
            }
        });
    }

    return {
        getProductId: function () {
            var productOptions = $('#product_addtocart_form').serializeJSON();
            return productOptions['product'];
        },

        parse: function () {
            var productOptions = $('#product_addtocart_form').serializeJSON();
            productOptions = _.pick(productOptions, 'super_attribute', 'options');

            return productOptionsProcessors.parse(productOptions);
        },

        apply: function (productOptions) {
            productOptionsProcessors.apply(productOptions);

            if (productOptions['super_attribute']) {
                applySuperAttribute(productOptions['super_attribute']);
            }

            if (productOptions['options']) {
                setTimeout(function () {
                    applyOptions(productOptions['options']);
                }, 400);
            }
        }
    };
});
