/**
 * Created by FindCanary, LLC.
 * Project: 4over
 */

define([
    'underscore',
    'Magento_Checkout/js/model/url-builder',
    'FourOver_Project/js/model/error-processor',
    'mage/storage'
], function (_, urlBuilder, errorProcessor, storage) {
    'use strict';

    return function toCart(payload) {
        payload = _.omit(payload, 'addresses_valid_ids');
        return storage.post(
            urlBuilder.createUrl('/project/to-cart', {}),
            JSON.stringify({transferProject: payload})
        ).fail(
            function (response) {
                errorProcessor.process(response);
            }
        );
    };
});
