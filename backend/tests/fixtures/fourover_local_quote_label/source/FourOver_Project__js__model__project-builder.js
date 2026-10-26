/**
 * Created by FindCanary, LLC.
 * Project: 4over
 */

define([
    'jquery',
    'underscore',
    'ko',
    'FourOver_Project/js/model/selected-runsize',
    'FourOver_ProductRunSize/js/model/selected-custom-run-size',
    'FourOver_Project/js/model/project-builder-extend',
    'FourOver_Project/js/action/generate-uuid',
    'FourOver_Project/js/model/project/shipment-builder',
    'FourOver_Project/js/model/project/artwork-status-manager'
], function ($, _, ko, selectedRunsize, selectedCustomRunSize, projectBuilderExtend, generateUuid, shipmentBuilder, artworkStatusManager) {
    'use strict';

    var defaultStage = 'default',
        defaultProductId = null,
        defaultRunSize = 1,
        defaultProductOptions = {};

    var initializeShipments = function (project, shipmentsData) {
        return _.map(shipmentsData, function (shipmentData) {
            return shipmentBuilder.build(project, shipmentData);
        });
    };

    var resetShipmentUuids = function (project, shipmentsData) {
        return _.map(shipmentsData, function (shipmentData) {
            return shipmentBuilder.resetUuid(project, shipmentBuilder.parse(shipmentData));
        });
    };

    var parseShipments = function (shipments) {
        return _.map(shipments, shipmentBuilder.parse);
    };

    var handleArtWorkStatus = function (project, updateOnIframe = false) {
        if (document.readyState === "complete" &&
            (updateOnIframe || !$("#dm-iframe").is(':visible')) &&
            $('body').hasClass('catalog-product-view-direct-mail')
        ) {
			
            artworkStatusManager.setStatus(project, 'Not Submitted');
        }
    };

    return {
        parse: function (project) {
            let projectData = {
                state: project().state,
                uuid: project().uuid,
                product_id: project().productId,
                name: project().name(),
                run_size: project().runSize(),
                custom_run_size: project().customRunSize(),
                product_options: project().productOptions(),
                shipments: parseShipments(project().shipments())
            };

            projectBuilderExtend.parse(project, projectData);
            return projectData;
        },

        reset: function (project, projectData) {
            projectData = projectData || {};

            project().state = (projectData.state || defaultStage);
            project().uuid = projectData.uuid || generateUuid();
            project().productId = projectData.product_id || defaultProductId;
            project().name((projectData.name));
            project().runSize((projectData.run_size || defaultRunSize));
            project().customRunSize((projectData.custom_run_size || 0));
            project().productOptions((projectData.product_options || defaultProductOptions));

            project().shipments(initializeShipments(project(), (projectData.shipments || [])));
            if (project().shipments().length === 0) {
                project().shipments.push(shipmentBuilder.build(project()));
            }
        },

        resetUuid: function (project) {
            project().uuid = generateUuid();
            project().state = defaultStage;
            project().shipments(resetShipmentUuids(project(), (project().shipments() || [])));
            if (project().shipments().length === 0) {
                project().shipments.push(shipmentBuilder.build(project()));
            }
        },

        build: function () {
            let project = {
                state: defaultStage,
                uuid: generateUuid(),
                productId: defaultProductId,
                name: ko.observable(''),
                runSize: selectedRunsize,
                productOptions: ko.observable(defaultProductOptions),
                customRunSize: selectedCustomRunSize
            };

            project.shipments = ko.observableArray([]);
            project.shipments.push(shipmentBuilder.build(project));

            projectBuilderExtend.build(project);
            return project;
        },

        initObservable: function (project) {
            project().name.subscribe(function () {
                handleArtWorkStatus(project,true);
                project.valueHasMutated();
            });
            project().runSize.subscribe(function () {
                handleArtWorkStatus(project,true);
                project.valueHasMutated();
            });
            project().customRunSize.subscribe(function () {
                project.valueHasMutated();
            });
            project().shipments.subscribe(function () {
                project.valueHasMutated();
            });
            project().productOptions.subscribe(function () {
                handleArtWorkStatus(project);
                project.valueHasMutated();
            });

            projectBuilderExtend.initObservable(project);

            return project;
        }
    }
});
