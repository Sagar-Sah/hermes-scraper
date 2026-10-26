/**
 * Created by FindCanary, LLC.
 * Project: 4over
 */

define([
    'underscore'
], function (_) {
    'use strict';

    var parsers = [];
    var appliers = [];

    return {
        addParser: function (parser) {
            parsers.push(parser);
        },

        parse: function (productOptions) {
            _.each(parsers, function (parser) {
                productOptions = parser.parse(productOptions);
            });
            return productOptions;
        },

        addApplier: function (applier) {
            appliers.push(applier);
        },

        apply: function (productOptions) {
            _.each(appliers, function (applier) {
                applier.apply(productOptions);
            });
        }
    };
});
