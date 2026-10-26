/**
 * Created by FindCanary, LLC.
 * Project: 4over
 */

define([
    'underscore',
    'FourOver_Project/js/action/project/load-addresses'
], function (_, loadProjectAddresses) {
    'use strict';

    var addressList = [];

    return {
        getAddressById: function (addressId) {
            return _.findWhere(addressList, {id: ('' + addressId)}) || null;
        },

        getDefaultShipping: function () {
            return _.findWhere(addressList, {default_shipping: true}) || null;
        },

        loadProjectAddresses: function (addressIds) {
            return loadProjectAddresses(addressIds)
                .done(function (addresses) {
                    addressList = addresses;
                }.bind(this));
        }
    };
});
