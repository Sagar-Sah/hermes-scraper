/**
 * Created by FindCanary, LLC.
 * Project: 4over
 */

define([
    'ko',
    'FourOver_Project/js/model/project-builder'
], function (ko, projectBuilder) {
    'use strict';

    var project = ko.observable(projectBuilder.build());
    projectBuilder.initObservable(project);
    return project;
});
