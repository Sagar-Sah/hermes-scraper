/**
 * Created by FindCanary, LLC.
 * Project: 4over
 */

define([
    'Magento_Checkout/js/model/url-builder',
    'mage/storage'
], function (urlBuilder, storage) {
    'use strict';

    return function getList(addressIds) {
        return storage.post(
            urlBuilder.createUrl('/project/mine/addresses', {}),
            JSON.stringify({addressIds: addressIds})
        );
    };
});
