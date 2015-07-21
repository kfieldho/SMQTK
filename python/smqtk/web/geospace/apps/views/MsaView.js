var tempus = tempus || {};

tempus.MsaView = Backbone.View.extend({
    initialize: function(options) {
        this.render(options);
    },

    render: function(options) {
        var shape = this.model.get('shape');

        // Create an msa feature layer if one doesn't already exist on the map
        if (!_.has(tempus.mapView, 'msaFeatureLayer')) {
            tempus.mapView.msaFeatureLayer = tempus.map.createLayer('feature');
        }
        // Let the user override any properties on the shape, useful for styling
        if (options.shapeFilter) {
            shape = options.shapeFilter(shape);
        }

        geo.createFileReader('jsonReader', {
            layer: tempus.mapView.msaFeatureLayer
        }).read(JSON.stringify(shape), function() {
            tempus.map.draw();
        });
    }
});
